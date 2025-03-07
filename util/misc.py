# ------------------------------------------------------------------------
# SiameseIM
# Copyright (c) SenseTime. All Rights Reserved.
# ------------------------------------------------------------------------
# Modified from MAE (https://github.com/facebookresearch/mae)
# Copyright (c) Meta Platforms, Inc. and affiliates. All Rights Reserved.
# ------------------------------------------------------------------------
# References:
# DeiT: https://github.com/facebookresearch/deit
# BEiT: https://github.com/microsoft/unilm/tree/master/beit
# ------------------------------------------------------------------------


import builtins
import datetime
import os
import io
import time
from collections import defaultdict, deque
from pathlib import Path

import torch
import torch.distributed as dist
from torch import inf
import torch.nn as nn
import torch.nn.functional as F


class SmoothedValue(object):
    """Track a series of values and provide access to smoothed values over a
    window or the global series average.
    """

    def __init__(self, window_size=20, fmt=None):
        if fmt is None:
            fmt = "{median:.4f} ({global_avg:.4f})"
        self.deque = deque(maxlen=window_size)
        self.total = 0.0
        self.count = 0
        self.fmt = fmt

    def update(self, value, n=1):
        self.deque.append(value)
        self.count += n
        self.total += value * n

    def synchronize_between_processes(self):
        """
        Warning: does not synchronize the deque!
        """
        if not is_dist_avail_and_initialized():
            return
        t = torch.tensor([self.count, self.total], dtype=torch.float64, device='cuda')
        dist.barrier()
        dist.all_reduce(t)
        t = t.tolist()
        self.count = int(t[0])
        self.total = t[1]

    @property
    def median(self):
        d = torch.tensor(list(self.deque))
        return d.median().item()

    @property
    def avg(self):
        d = torch.tensor(list(self.deque), dtype=torch.float32)
        return d.mean().item()

    @property
    def global_avg(self):
        return self.total / self.count

    @property
    def max(self):
        return max(self.deque)

    @property
    def value(self):
        return self.deque[-1]

    def __str__(self):
        return self.fmt.format(
            median=self.median,
            avg=self.avg,
            global_avg=self.global_avg,
            max=self.max,
            value=self.value)


class MetricLogger(object):
    def __init__(self, delimiter="\t"):
        self.meters = defaultdict(SmoothedValue)
        self.delimiter = delimiter

    def update(self, **kwargs):
        for k, v in kwargs.items():
            if v is None:
                continue
            if isinstance(v, torch.Tensor):
                v = v.item()
            assert isinstance(v, (float, int))
            self.meters[k].update(v)

    def __getattr__(self, attr):
        if attr in self.meters:
            return self.meters[attr]
        if attr in self.__dict__:
            return self.__dict__[attr]
        raise AttributeError("'{}' object has no attribute '{}'".format(
            type(self).__name__, attr))

    def __str__(self):
        loss_str = []
        for name, meter in self.meters.items():
            loss_str.append(
                "{}: {}".format(name, str(meter))
            )
        return self.delimiter.join(loss_str)

    def synchronize_between_processes(self):
        for meter in self.meters.values():
            meter.synchronize_between_processes()

    def add_meter(self, name, meter):
        self.meters[name] = meter

    def log_every(self, iterable, print_freq, header=None):
        i = 0
        if not header:
            header = ''
        start_time = time.time()
        end = time.time()
        iter_time = SmoothedValue(fmt='{avg:.4f}')
        data_time = SmoothedValue(fmt='{avg:.4f}')
        space_fmt = ':' + str(len(str(len(iterable)))) + 'd'
        log_msg = [
            header,
            '[{0' + space_fmt + '}/{1}]',
            'eta: {eta}',
            '{meters}',
            'time: {time}',
            'data: {data}'
        ]
        if torch.cuda.is_available():
            log_msg.append('max mem: {memory:.0f}')
        log_msg = self.delimiter.join(log_msg)
        MB = 1024.0 * 1024.0
        for obj in iterable:
            data_time.update(time.time() - end)
            yield obj
            iter_time.update(time.time() - end)
            if i % print_freq == 0 or i == len(iterable) - 1:
                eta_seconds = iter_time.global_avg * (len(iterable) - i)
                eta_string = str(datetime.timedelta(seconds=int(eta_seconds)))
                if torch.cuda.is_available():
                    print(log_msg.format(
                        i, len(iterable), eta=eta_string,
                        meters=str(self),
                        time=str(iter_time), data=str(data_time),
                        memory=torch.cuda.max_memory_allocated() / MB))
                else:
                    print(log_msg.format(
                        i, len(iterable), eta=eta_string,
                        meters=str(self),
                        time=str(iter_time), data=str(data_time)))
            i += 1
            end = time.time()
        total_time = time.time() - start_time
        total_time_str = str(datetime.timedelta(seconds=int(total_time)))
        print('{} Total time: {} ({:.4f} s / it)'.format(
            header, total_time_str, total_time / len(iterable)))


def setup_for_distributed(is_master):
    """
    This function disables printing when not in master process
    """
    builtin_print = builtins.print

    def print(*args, **kwargs):
        # force = kwargs.pop('force', False)
        # force = force or (get_world_size() > 8)
        if is_master:
            now = datetime.datetime.now().time()
            builtin_print('[{}] '.format(now), end='')  # print with time stamp
            builtin_print(*args, **kwargs)

    builtins.print = print


def is_dist_avail_and_initialized():
    if not dist.is_available():
        return False
    if not dist.is_initialized():
        return False
    return True


def get_world_size():
    if not is_dist_avail_and_initialized():
        return 1
    return dist.get_world_size()


def get_rank():
    if not is_dist_avail_and_initialized():
        return 0
    return dist.get_rank()


def is_main_process():
    return get_rank() == 0


def save_on_master(*args, **kwargs):
    if is_main_process():
        torch.save(*args, **kwargs)

def init_distributed_mode(local_rank, args):
    ngpus_per_node = torch.cuda.device_count()
    # args.env.distributed = ngpus_per_node > 0
    if args.env.distributed:
        args.env.world_size = ngpus_per_node * args.env.world_size
        args.env.rank = args.env.rank * ngpus_per_node + local_rank
    else:
        print('Not using distributed mode')
        setup_for_distributed(is_master=True)  # hack
        args.env.world_size = 1
        args.env.rank = 0
        return

    print(args.env.dist_backend, args.env.dist_url, args.env.world_size, args.env.rank)
    dist.init_process_group(backend=args.env.dist_backend, init_method=args.env.dist_url,
                            world_size=args.env.world_size, rank=args.env.rank)

    torch.cuda.set_device(local_rank)
    print('Distributed init (rank {}): {}, gpu {}'.format(
        args.env.rank, args.env.dist_url, local_rank), flush=True)
    torch.distributed.barrier()
    setup_for_distributed(args.env.rank == 0 and local_rank == 0)

# def init_distributed_mode(args):
#     if args.dist_on_itp:
#         args.rank = int(os.environ['OMPI_COMM_WORLD_RANK'])
#         args.world_size = int(os.environ['OMPI_COMM_WORLD_SIZE'])
#         args.gpu = int(os.environ['OMPI_COMM_WORLD_LOCAL_RANK'])
#         args.dist_url = "tcp://%s:%s" % (os.environ['MASTER_ADDR'], os.environ['MASTER_PORT'])
#         os.environ['LOCAL_RANK'] = str(args.gpu)
#         os.environ['RANK'] = str(args.rank)
#         os.environ['WORLD_SIZE'] = str(args.world_size)
#         # ["RANK", "WORLD_SIZE", "MASTER_ADDR", "MASTER_PORT", "LOCAL_RANK"]
#     elif 'RANK' in os.environ and 'WORLD_SIZE' in os.environ:
#         args.rank = int(os.environ["RANK"])
#         args.world_size = int(os.environ['WORLD_SIZE'])
#         args.gpu = int(os.environ['LOCAL_RANK'])
#     elif 'SLURM_PROCID' in os.environ:
#         args.rank = int(os.environ['SLURM_PROCID'])
#         args.world_size = int(os.environ['SLURM_NTASKS'])
#         node_list = os.environ['SLURM_STEP_NODELIST']
#         num_gpus = torch.cuda.device_count()
#         args.gpu = args.rank % torch.cuda.device_count()
#         torch.cuda.set_device(args.rank % num_gpus)
#         import subprocess
#         addr = subprocess.getoutput(
#             f'scontrol show hostname {node_list} | head -n1')
#         # specify master port
#         if hasattr(args, 'port'):
#             os.environ['MASTER_PORT'] = str(args.port)
#         elif 'MASTER_PORT' in os.environ:
#             pass  # use MASTER_PORT in the environment variable
#         else:
#             # 29500 is torch.distributed default port
#             os.environ['MASTER_PORT'] = '28506'
#         # use MASTER_ADDR in the environment variable if it already exists
#         if 'MASTER_ADDR' not in os.environ:
#             os.environ['MASTER_ADDR'] = addr
#         os.environ['WORLD_SIZE'] = str(args.world_size)
#         os.environ['LOCAL_RANK'] = str(args.rank % num_gpus)
#         os.environ['LOCAL_SIZE'] = str(num_gpus)
#         os.environ['RANK'] = str(args.rank)
#         # dist.init_process_group(backend='nccl')
#     else:
#         print('Not using distributed mode')
#         setup_for_distributed(is_master=True)  # hack
#         args.env.distributed = False
#         return

#     args.env.distributed = True

#     torch.cuda.set_device(args.gpu)
#     args.dist_backend = 'nccl'
#     print('| distributed init (rank {}): {}, gpu {}'.format(
#         args.rank, args.dist_url, args.gpu), flush=True)
#     torch.distributed.init_process_group(backend=args.dist_backend, init_method=args.dist_url,
#                                          world_size=args.world_size, rank=args.rank)
#     torch.distributed.barrier()
#     setup_for_distributed(args.rank == 0)


class NativeScalerWithGradNormCount:
    state_dict_key = "amp_scaler"

    def __init__(self, enabled=True, growth_interval=2000):
        self.enabled = enabled
        self._scaler = torch.cuda.amp.GradScaler(
            enabled=enabled, growth_interval=growth_interval)

    def __call__(self, loss, optimizer, clip_grad=None, parameters=None, create_graph=False, update_grad=True):
        self._scaler.scale(loss).backward(create_graph=create_graph)
        if update_grad:
            if clip_grad is not None:
                assert parameters is not None
                self._scaler.unscale_(optimizer)  # unscale the gradients of optimizer's assigned params in-place
                norm = torch.nn.utils.clip_grad_norm_(parameters, clip_grad)
            else:
                self._scaler.unscale_(optimizer)
                norm = get_grad_norm_(parameters)
            self._scaler.step(optimizer)
            self._scaler.update()
        else:
            norm = None
        return norm

    def state_dict(self):
        return self._scaler.state_dict()

    def load_state_dict(self, state_dict):
        if self.enabled:
            self._scaler.load_state_dict(state_dict)


def get_grad_norm_(parameters, norm_type: float = 2.0) -> torch.Tensor:
    if isinstance(parameters, torch.Tensor):
        parameters = [parameters]
    parameters = [p for p in parameters if p.grad is not None]
    norm_type = float(norm_type)
    if len(parameters) == 0:
        return torch.tensor(0.)
    device = parameters[0].grad.device
    if norm_type == inf:
        total_norm = max(p.grad.detach().abs().max().to(device) for p in parameters)
    else:
        total_norm = torch.norm(torch.stack([torch.norm(p.grad.detach(), norm_type).to(device) for p in parameters]), norm_type)
    return total_norm


def save_model(args, epoch, model, model_without_ddp, optimizer, loss_scaler,
               latest=False,
               latest_postfix='latest'):
    output_dir = Path(args.output_dir)
    epoch_name = str(epoch)
    if loss_scaler is not None:
        checkpoint_paths = []
        if latest:
            checkpoint_paths = [output_dir / (f'checkpoint-{latest_postfix}.pth')]
        else:
            checkpoint_paths = [output_dir / ('checkpoint-%s.pth' % epoch_name)]
        to_save = {
            'model': model_without_ddp.state_dict(),
            'optimizer': optimizer.state_dict(),
            'epoch': epoch,
            'scaler': loss_scaler.state_dict(),
            'args': args,
        }
        for checkpoint_path in checkpoint_paths:
            save_on_master(to_save, checkpoint_path)
    else:
        client_state = {'epoch': epoch}
        model.save_checkpoint(save_dir=args.output_dir, tag="checkpoint-%s" % epoch_name, client_state=client_state)


def load_model(args, model_without_ddp, optimizer, loss_scaler):
    if args.resume:
        if args.resume.startswith('https'):
            checkpoint = torch.hub.load_state_dict_from_url(
                args.resume, map_location='cpu', check_hash=True)
        else:
            checkpoint = torch.load(args.resume, map_location='cpu')
        model_without_ddp.load_state_dict(checkpoint['model'])
        print("Resume checkpoint %s" % args.resume)
        if 'optimizer' in checkpoint and 'epoch' in checkpoint and not (hasattr(args, 'eval') and args.eval):
            optimizer.load_state_dict(checkpoint['optimizer'])
            args.start_epoch = checkpoint['epoch'] + 1
            if 'scaler' in checkpoint:
                loss_scaler.load_state_dict(checkpoint['scaler'])
            print("With optim & sched!")


def auto_load_model(args, model_without_ddp, optimizer, loss_scaler):
    # torch.amp
    output_dir = Path(args.output_dir)

    if args.auto_resume and len(args.resume) == 0:
        import glob
        all_checkpoints = glob.glob(os.path.join(output_dir, 'checkpoint-*.pth'))
        latest_ckpt = -1
        for ckpt in all_checkpoints:
            t = ckpt.split('-')[-1].split('.')[0]
            if t.isdigit():
                latest_ckpt = max(int(t), latest_ckpt)
        if latest_ckpt >= 0:
            args.resume = os.path.join(output_dir, 'checkpoint-%d.pth' % latest_ckpt)
        if os.path.exists(os.path.join(output_dir, 'checkpoint-latest.pth')):
            args.resume = os.path.join(output_dir, 'checkpoint-latest.pth')
        print("Auto resume checkpoint: %s" % args.resume)
    
    if args.resume:
        if args.resume.startswith('https'):
            checkpoint = torch.hub.load_state_dict_from_url(
                args.resume, map_location='cpu', check_hash=True)
        else:
            checkpoint = torch.load(args.resume, map_location='cpu')
        model_without_ddp.load_state_dict(checkpoint['model'], strict=False)
        print("Resume checkpoint %s" % args.resume)
        if 'optimizer' in checkpoint and 'epoch' in checkpoint:
            optimizer.load_state_dict(checkpoint['optimizer'])
            args.start_epoch = checkpoint['epoch'] + 1
            if 'scaler' in checkpoint:
                loss_scaler.load_state_dict(checkpoint['scaler'])
            print("With optim & sched!")


def all_reduce_mean(x):
    world_size = get_world_size()
    if world_size > 1:
        x_reduce = torch.tensor(x).cuda()
        dist.all_reduce(x_reduce)
        x_reduce /= world_size
        return x_reduce.item()
    else:
        return x


class LayerNorm(nn.LayerNorm):

    @torch.cuda.amp.autocast(enabled=False)
    def forward(self, input):
        return super(LayerNorm, self).forward(input.float())


def add_lr_weight_decay(model, weight_decay=1e-5, lr=1e-4, skip_list=()):
    decay = []
    no_decay = []
    no_decay_names = []
    decay_small_lr = []
    decay_small_lr_names = []
    for name, param in model.named_parameters():
        if not param.requires_grad:
            continue  # frozen weights
        if 'offset' in name:
            decay_small_lr.append(param)
            decay_small_lr_names.append(name)

        elif len(param.shape) == 1 or name.endswith(".bias") or name in skip_list:
            no_decay.append(param)
            no_decay_names.append(name)
        else:
            decay.append(param)
    print(f'decay_small_lr_names: {decay_small_lr_names}')
    print(f'no_decay_names: {no_decay_names}')
    return [
        {'params': no_decay, 'weight_decay': 0., 'lr': lr},
        {'params': decay, 'weight_decay': weight_decay, 'lr': lr},
        {'params': decay_small_lr, 'weight_decay': weight_decay, 'lr': lr*0.1},
    ]



import math
from torch.utils.data.sampler import Sampler


class NodeDistributedSampler(Sampler):
    """Sampler that restricts data loading to a subset of the dataset.
    It is especially useful in conjunction with
    :class:`torch.nn.parallel.DistributedDataParallel`. In such case, each
    process can pass a DistributedSampler instance as a DataLoader sampler,
    and load a subset of the original dataset that is exclusive to it.
    .. note::
        Dataset is assumed to be of constant size.
    Arguments:
        dataset: Dataset used for sampling.
        num_replicas (optional): Number of processes participating in
            distributed training.
        rank (optional): Rank of the current process within num_replicas.
    """

    def __init__(self, dataset, num_replicas=None, rank=None, local_rank=None, local_size=None, shuffle=True):
        if num_replicas is None:
            if not dist.is_available():
                raise RuntimeError("Requires distributed package to be available")
            num_replicas = dist.get_world_size()
        if rank is None:
            if not dist.is_available():
                raise RuntimeError("Requires distributed package to be available")
            rank = dist.get_rank()
        if local_rank is None:
            local_rank = int(os.environ.get('LOCAL_RANK', 0))
        if local_size is None:
            local_size = int(os.environ.get('LOCAL_SIZE', 1))
        self.dataset = dataset
        self.shuffle = shuffle
        self.num_replicas = num_replicas
        self.num_parts = local_size
        self.rank = rank
        self.local_rank = local_rank
        self.epoch = 0
        self.num_samples = int(math.ceil(len(self.dataset) * 1.0 / self.num_replicas))
        self.total_size = self.num_samples * self.num_replicas

        self.total_size_parts = self.num_samples * self.num_replicas // self.num_parts

    def __iter__(self):
        if self.shuffle:
            # deterministically shuffle based on epoch
            g = torch.Generator()
            g.manual_seed(self.epoch)
            indices = torch.randperm(len(self.dataset), generator=g).tolist()
        else:
            indices = torch.arange(len(self.dataset)).tolist()
        indices = [i for i in indices if i % self.num_parts == self.local_rank]

        # add extra samples to make it evenly divisible
        indices += indices[:(self.total_size_parts - len(indices))]
        assert len(indices) == self.total_size_parts

        # subsample
        indices = indices[self.rank // self.num_parts:self.total_size_parts:self.num_replicas // self.num_parts]
        assert len(indices) == self.num_samples

        return iter(indices)

    def __len__(self):
        return self.num_samples

    def set_epoch(self, epoch):
        self.epoch = epoch


class GatherLayer(torch.autograd.Function):
    """Gather tensors from all process, supporting backward propagation.
    """

    @staticmethod
    def forward(ctx, input):
        ctx.save_for_backward(input)
        output = [torch.zeros_like(input) for _ in range(dist.get_world_size())]
        dist.all_gather(output, input)
        return torch.stack(output, 0)

    @staticmethod
    def backward(ctx, grads):
        input, = ctx.saved_tensors
        dist.all_reduce(grads)
        grad_out = torch.zeros_like(input)
        grad_out[:] = grads[dist.get_rank()]
        return grad_out


class LabelSmoothingCrossEntropy(nn.Module):
    """
    NLL loss with label smoothing.
    """
    def __init__(self, smoothing=0.1):
        """
        Constructor for the LabelSmoothing module.
        :param smoothing: label smoothing factor
        """
        super(LabelSmoothingCrossEntropy, self).__init__()
        assert smoothing < 1.0
        self.smoothing = smoothing
        self.confidence = 1. - smoothing

    def forward(self, x, target, reduction='mean'):
        logprobs = F.log_softmax(x, dim=-1)
        nll_loss = -logprobs.gather(dim=-1, index=target.unsqueeze(1))
        nll_loss = nll_loss.squeeze(1)
        smooth_loss = -logprobs.mean(dim=-1)
        loss = self.confidence * nll_loss + self.smoothing * smooth_loss
        if reduction == 'mean':
            return loss.mean()
        elif reduction == 'none':
            return loss
        else:
            raise NotImplementedError


class LabelSmoothingCrossEntropyWithSoftTarget(nn.Module):
    """
    NLL loss with label smoothing.
    """
    def __init__(self, smoothing=0.1):
        """
        Constructor for the LabelSmoothing module.
        :param smoothing: label smoothing factor
        """
        super(LabelSmoothingCrossEntropyWithSoftTarget, self).__init__()
        assert smoothing < 1.0
        self.smoothing = smoothing
        self.confidence = 1. - smoothing

    def forward(self, x, target, reduction='mean'):
        logprobs = F.log_softmax(x, dim=-1)
        # nll_loss = -logprobs.gather(dim=-1, index=target.unsqueeze(1))
        # nll_loss = nll_loss.squeeze(1)
        nll_loss = - (logprobs * target).sum(dim=-1)
        smooth_loss = -logprobs.mean(dim=-1)
        loss = self.confidence * nll_loss + self.smoothing * smooth_loss
        if reduction == 'mean':
            return loss.mean()
        elif reduction == 'none':
            return loss
        else:
            raise NotImplementedError


class CheckpointManager:
    def __init__(self,
                 modules,
                 ckpt_dir,
                 epochs,
                 save_freq=None,
                 suffix='',
                 save_list=[]):
        self.modules = modules
        self.ckpt_dir = ckpt_dir
        self.epochs = epochs
        self.save_freq = save_freq
        self.suffix = suffix

        self.distributed = dist.is_available() and dist.is_initialized()
        self.world_size = dist.get_world_size() if self.distributed else 1
        self.rank = dist.get_rank() if self.distributed else 0

        self.save_list = save_list

        if self.rank == 0:
            os.makedirs(os.path.join(self.ckpt_dir), exist_ok=True)

    def resume(self):
        ckpt_fname = os.path.join(self.ckpt_dir, f'checkpoint_latest{self.suffix}.pth')
        start_epoch = 0
        if os.path.isfile(ckpt_fname):
            checkpoint = torch.load(ckpt_fname, map_location='cpu')

            # Load state dict
            for k in self.modules:
                self.modules[k].load_state_dict(checkpoint[k])
                # try:
                #     self.modules[k].load_state_dict(checkpoint[k])
                # except KeyError:
                #     self.modules[k].load_state_dict(checkpoint['model'])
            start_epoch = checkpoint['epoch']
            print(f"=> loaded checkpoint '{ckpt_fname}' (epoch {checkpoint['epoch']})")
        else:
            print(f"=> no checkpoint found at '{ckpt_fname}'")

        return start_epoch

    def create_state_dict(self, save_dict=None):
        state = {k: self.modules[k].state_dict() for k in self.modules}
        if save_dict is not None:
            state.update(save_dict)
        return state

    def checkpoint(self, epoch, save_dict=None):
        if self.rank != 0:
            return
        state = self.create_state_dict(save_dict)
        ckpt_fname = os.path.join(self.ckpt_dir, f'checkpoint_latest{self.suffix}.pth')
        torch.save(state, ckpt_fname)
        print(f"=> saved checkpoint '{ckpt_fname}' (epoch {epoch})")

        if self.save_freq is not None and ((epoch % self.save_freq == 0) or epoch in self.save_list):
            ckpt_fname = os.path.join(self.ckpt_dir, f'checkpoint_{epoch:04d}{self.suffix}.pth')
            torch.save(state, ckpt_fname)
            print(f"=> saved checkpoint '{ckpt_fname}' (epoch {epoch})")

    def convert_det_ckpt(self):
        """convert to the detectron2 checkpoint
        """
        checkpoint = self.create_state_dict()
        checkpoint_model = checkpoint['state_dict']
        # print(list(checkpoint_model.keys()))
        # retain only base_encoder up to before the embedding layer
        for k in list(checkpoint_model.keys()):
            # if k.startswith('module.encoder.embed.'):
            #     # rename from embed to patch_embed # ntd for path1_maefeat only
            #     checkpoint_model["patch_embed."+k[len("module.encoder.embed."):]] = checkpoint_model[k]
            if k.startswith('module.encoder.') and k!="module.encoder.mask_token":
                # remove prefix
                checkpoint_model[k[len("module.encoder."):]] = checkpoint_model[k]
            if k.startswith('encoder.') and k!="encoder.mask_token":
                # remove prefix
                checkpoint_model[k[len("encoder."):]] = checkpoint_model[k]
            # delete renamed or unused k
            del checkpoint_model[k]

        # modify the patch embed: linear to 2D CNN
        cnn_weight = checkpoint_model['patch_embed.proj.weight'].reshape(-1,16,16,3)
        cnn_weight = torch.einsum('lpqc->lcpq', cnn_weight)

        checkpoint_model['patch_embed.proj.weight'] = cnn_weight
        
        state = {"model": checkpoint_model}
        tgt_path = os.path.join(self.ckpt_dir, f'checkpoint_latest_detectron2.pth')
        torch.save(state, tgt_path)
        print(f"=> saved detectron2 checkpoint '{tgt_path}'")


def avg_pairwise_norm_dist(q,k,name=""):
    import torch.nn.functional as F

    q = F.normalize(q, p=2, dim=-1)
    k = F.normalize(k, p=2, dim=-1)

    if len(q.shape) == 3 and len(k.shape) == 3:
        dist = 2-2*torch.einsum('npd,nqd->npq', q, k)
        return {name : dist.mean().item()}
    else:
        dist = 2-2*torch.einsum('nhpd,nhqd->nhpq', q, k)
        return {name+str(h) : dist[:,h].mean().item() for h in range(q.shape[1])}
    
def linear2cnn(linear_weight):
    # modify the patch embed: linear to 2D CNN
    return torch.einsum('lpqc->lcpq', linear_weight.reshape(-1,16,16,3))

def cnn2linear(cnn_weight):
    return torch.einsum('lcpq->lpqc', cnn_weight).reshape(-1,16*16*3)


def init_wandb(args, job_dir, entity='pmorgado', project='mae-vs-mclr', job_name='tmp'):
    import wandb
    wandb_dir = os.path.join(job_dir, 'wandb')
    os.makedirs(wandb_dir, exist_ok=True)
    runid = None
    if os.path.exists(f"{wandb_dir}/runid.txt"):
        runid = open(f"{wandb_dir}/runid.txt").read()
    wandb.init(project=project,
               name=job_name,
               dir=wandb_dir,
               entity=entity,
               resume="allow",
               id=runid)
    open(f"{wandb_dir}/runid.txt", 'w').write(wandb.run.id)
    wandb.config.update({k: args[k] for k in args if k not in wandb.config})

@torch.no_grad()
def concat_all_gather(tensor):
    """
    Performs all_gather operation on the provided tensors.
    *** Warning ***: torch.distributed.all_gather has no gradient.
    """
    if not torch.distributed.is_initialized():
        return tensor
    tensors_gather = [torch.ones_like(tensor)
                      for _ in range(torch.distributed.get_world_size())]
    torch.distributed.all_gather(tensors_gather, tensor, async_op=False)

    output = torch.cat(tensors_gather, dim=0)
    return output

@torch.no_grad()
def eval_knn(eval_loader, model, epoch, args, device, bn=False):
    from torch.nn import functional as F
    from copy import deepcopy
    print(f'==> Begin evaluation epoch {epoch}')
    print_freq = args.log.print_freq

    metric_logger = MetricLogger(delimiter="  ")
    
    try:
        model = deepcopy(model.module)  #if args.env.distributed else model
    except:
        model = deepcopy(model)

    # Extract features
    features, labels = [], []
    for images, y in metric_logger.log_every(eval_loader, print_freq, 'Extract features'):
        images, y = images.to(device, non_blocking=True), y.to(device, non_blocking=True)
        _features = model.forward_features(images, feature=args.knn_feature)
        if bn:
            bn = torch.nn.BatchNorm1d(_features.shape[-1], affine=False).to(device)
            _features = bn(_features)
        features.append(_features)                
        labels.append(y)

    # Synchronize across gpus
    features = concat_all_gather(F.normalize(torch.cat(features), p=2, dim=1))
    labels = concat_all_gather(torch.cat(labels))

    # kNN Evaluation
    metric_logger = MetricLogger(delimiter="  ")
    metric_logger.add_meter('Acc1', SmoothedValue(fmt='avg:6.3f'))
    for i in metric_logger.log_every(range(0, features.shape[0], args.batch_size), 250):
        qfeats = features[i:i+args.batch_size]
        qlbls = labels[i:i+args.batch_size]
        scores = torch.einsum('qd,nd->qn', qfeats, features)
        topk_idx = torch.topk(scores, k=2, dim=1, sorted=True)[1][:, 1]
        topk_lbl = labels[topk_idx]

        acc1 = (topk_lbl == qlbls).float().mean()*100
        metric_logger.update(Acc1=acc1, n=qlbls.shape[0])

    metric_logger.synchronize_between_processes()
    print(f"NN Acc1: {metric_logger.meters['Acc1'].global_avg:6.3f}")
    return metric_logger.meters['Acc1'].global_avg
