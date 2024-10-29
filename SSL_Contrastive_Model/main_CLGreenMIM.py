# Modified by Lang Huang (laynehuang@outlook.com)
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
# --------------------------------------------------------
# References:
# DeiT: https://github.com/facebookresearch/deit
# BEiT: https://github.com/microsoft/unilm/tree/master/beit
# MAE: https://github.com/facebookresearch/mae
# --------------------------------------------------------
import argparse
import datetime
import json
import numpy as np
import os
import time
from pathlib import Path
import torch.profiler
import torch
import torch.backends.cudnn as cudnn
from torch.utils.tensorboard import SummaryWriter
import torchvision.transforms as transforms
import torchvision.datasets as datasets
from util.mutimodal_dataset_sl import get_train_dataset_class,get_test_dataset_class
import timm.optim.optim_factory as optim_factory

import util.misc as misc
from util.misc import NativeScalerWithGradNormCount as NativeScaler
from utils.callbacks import EarlyStop
from engine_CLGreenMIM import train_one_epoch,evaluate
from modeling import get_pretrain_model
from modeling.greenMIM_CL_models import CLMaskedAutoencoder

def get_args_parser():
    parser = argparse.ArgumentParser('GreenMIM CL training', add_help=False)
    parser.add_argument('--batch_size', default=64, type=int,
                        help='Batch size per GPU (effective batch size is batch_size * accum_iter * # gpus')
    parser.add_argument('--epochs', default=400, type=int)
    parser.add_argument('--accum_iter', default=1, type=int,
                        help='Accumulate gradient iterations (for increasing the effective batch size under memory constraints)')
    parser.add_argument('--resizeshape',default=None,type=int,help='resize shape')
    # Model parameters
    parser.add_argument('--model', default='green_mim_swin_base_patch4_dec512b1', type=str, metavar='MODEL',
                        help='Name of model to train')
    parser.add_argument('--embedding_dim', default=1024, type=int)
    parser.add_argument('--img_size1',default=224,type=int)
    parser.add_argument('--img_size2',default=224,type=int)
    parser.add_argument('--img_size',default=224,type=int)

    parser.add_argument('--input_size', default=224, type=int,
                        help='images input size')

    parser.add_argument('--mask_ratio', default=0.75, type=float,
                        help='Masking ratio (percentage of removed patches).')

    parser.add_argument('--norm_pix_loss', action='store_true',
                        help='Use (per-patch) normalized pixels as targets for computing loss')
    parser.set_defaults(norm_pix_loss=False)
    parser.add_argument('--train_data_path',
                        default="/mnt/data2/ECG_CMR/toy_data/test_ECGlaCMR_data_dict_v11_toy.pt",
                        type=str,
                        help='dataset path')
    parser.add_argument('--test_data_path',
                        default="/mnt/data2/ECG_CMR/toy_data/test_ECGlaCMR_data_dict_v11_toy.pt",
                        type=str,
                        help='test dataset path')
    parser.add_argument('--dataset',default='mutimodal_dataset_CMRlaCMR',type=str)
    parser.add_argument('--ecg_input_size', type=tuple, default=(12, 5000))

    parser.add_argument('--timeFlip', type=float, default=0.33)

    parser.add_argument('--signFlip', type=float, default=0.33)
    #downstream task
    parser.add_argument('--downstream', default='regression', type=str, help='downstream task')
    parser.add_argument('--regression_dim',default=82,type=int,help='regression_dim')
    parser.add_argument('--classification_dis',default='I21',type=str,help='classification_dis')
    # Optimizer parameters
    parser.add_argument('--weight_decay', type=float, default=0.05,
                        help='weight decay (default: 0.05)')

    parser.add_argument('--lr', type=float, default=None, metavar='LR',
                        help='learning rate (absolute lr)')
    parser.add_argument('--blr', type=float, default=1e-4, metavar='LR',
                        help='base learning rate: absolute_lr = base_lr * total_batch_size / 256')
    parser.add_argument('--min_lr', type=float, default=0., metavar='LR',
                        help='lower lr bound for cyclic schedulers that hit 0')

    parser.add_argument('--warmup_epochs', type=int, default=40, metavar='N',
                        help='epochs to warmup LR')

    # Callback parameters
    parser.add_argument('--patience', default=10, type=float,
                        help='Early stopping whether val is worse than train for specified nb of epochs (default: -1, i.e. no early stopping)')
    parser.add_argument('--max_delta', default=0.005, type=float,
                        help='Early stopping threshold (val has to be worse than (train+delta)) (default: 0)')

    # Dataset parameters
    parser.add_argument('--data_path', default='/datasets01/imagenet_full_size/061417/', type=str,
                        help='dataset path')

    parser.add_argument('--output_dir', default="/mnt/data2/dingzhengyao/work/checkpoint/Newproject_v1/",
                        help='path where to save, empty for no saving')
    parser.add_argument('--log_dir', default='/mnt/data2/dingzhengyao/work/checkpoint/Newproject_v1/',
                        help='path where to tensorboard log')
    parser.add_argument('--device', default='cuda:2',
                        help='device to use for training / testing')
    parser.add_argument('--seed', default=0, type=int)
    parser.add_argument('--resume', default='',
                        help='resume from checkpoint')

    parser.add_argument('--start_epoch', default=0, type=int, metavar='N',
                        help='start epoch')
    parser.add_argument('--num_workers', default=8, type=int)
    parser.add_argument('--pin_mem', action='store_true',
                        help='Pin CPU memory in DataLoader for more efficient (sometimes) transfer to GPU.')
    parser.add_argument('--no_pin_mem', action='store_false', dest='pin_mem')
    parser.set_defaults(pin_mem=True)
    parser.add_argument('--save_freq', default=5, type=int)
    parser.add_argument('--select_modal',default='la_cmr_data')

    # distributed training parameters
    parser.add_argument('--world_size', default=1, type=int,
                        help='number of distributed processes')
    parser.add_argument('--local_rank', default=-1, type=int)
    parser.add_argument('--dist_on_itp', action='store_true')
    parser.add_argument('--dist_url', default='env://',
                        help='url used to set up distributed training')

    return parser


def main(args):
    

    print('job dir: {}'.format(os.path.dirname(os.path.realpath(__file__))))
    print("{}".format(args).replace(', ', ',\n'))

    device = torch.device(args.device)
    print(f'device:{device}')
    # fix the seed for reproducibility
    seed = args.seed
    torch.manual_seed(seed)
    np.random.seed(seed)
    cudnn.benchmark = True

    
    dataset_train = get_train_dataset_class(args.dataset,args)
    train_scaler = dataset_train.get_scaler()
    dataset_test = get_test_dataset_class(args.dataset,args,train_scaler)
    
    
    print(f'train set len:{len(dataset_train)}')
    print(f'test set len:{len(dataset_test)}')
    
    os.makedirs(args.log_dir, exist_ok=True)
    log_writer = SummaryWriter(log_dir=args.log_dir)
    

    data_loader_train = torch.utils.data.DataLoader(
        dataset_train, 
        batch_size=args.batch_size,
        num_workers=args.num_workers,
        pin_memory=args.pin_mem,
        drop_last=True,
        shuffle=True
    )
    data_loader_test = torch.utils.data.DataLoader(
        dataset_test, 
        batch_size=args.batch_size,
        num_workers=args.num_workers,
        pin_memory=args.pin_mem,
        drop_last=False,
        shuffle=False
    )
    # define the model
    args.img_size = args.img_size1
    model1 = get_pretrain_model(args.model,args=args)
    model1.to(device)
    args.img_size = args.img_size2
    model2 = get_pretrain_model(args.model,args=args)
    model2.to(device)

    model = CLMaskedAutoencoder(model1,model2,args)
    model.to(device)
    # print("Model = %s" % str(model))

    eff_batch_size = args.batch_size * args.accum_iter * misc.get_world_size()
    
    if args.lr is None:  # only base_lr is specified
        args.lr = args.blr * eff_batch_size / 256

    print("base lr: %.2e" % (args.lr * 256 / eff_batch_size))
    print("actual lr: %.2e" % args.lr)

    print("accumulate grad iterations: %d" % args.accum_iter)
    print("effective batch size: %d" % eff_batch_size)

    
    
    # following timm: set wd as 0 for bias and norm layers
    skip_list = [name for name, _ in model.named_parameters() if 'position_bias' in name or 'token' in name]
    print('skip_list = ', skip_list)
    param_groups = optim_factory.add_weight_decay(model, args.weight_decay, skip_list=skip_list)
    # param_groups = optim_factory.add_weight_decay(model_without_ddp, args.weight_decay, skip_list=skip_list)
    optimizer = torch.optim.AdamW(param_groups, lr=args.lr, betas=(0.9, 0.95))
    print(optimizer)
    loss_scaler = NativeScaler()

    misc.load_model(args=args, model_without_ddp=model, optimizer=optimizer, loss_scaler=loss_scaler)

    print(f"Start training for {args.epochs} epochs")
    start_time = time.time()
    
    eval_criterion = "loss"
    best_stats = {eval_criterion: np.inf}
    early_stop = EarlyStop(patience=args.patience, max_delta=args.max_delta)

    for epoch in range(args.start_epoch, args.epochs):
        train_stats = train_one_epoch(
            model, data_loader_train,
            optimizer, device, epoch, loss_scaler,
            log_writer=log_writer,
            args=args
        )
        

        log_stats = {**{f'train_{k}': v for k, v in train_stats.items()},
                        'epoch': epoch,}

        if args.output_dir and misc.is_main_process():
            if log_writer is not None:
                log_writer.flush()
            with open(os.path.join(args.output_dir, "log.txt"), mode="a", encoding="utf-8") as f:
                f.write(json.dumps(log_stats) + "\n")
        
        test_stats = evaluate(
            model, data_loader_test,
            optimizer, device, epoch, loss_scaler,
            log_writer=log_writer,
            args=args
        )

        log_stats = {**{f'test_{k}': v for k, v in test_stats.items()},
                        'epoch': epoch,}

        if args.output_dir and misc.is_main_process():
            if log_writer is not None:
                log_writer.flush()
            with open(os.path.join(args.output_dir, "log.txt"), mode="a", encoding="utf-8") as f:
                f.write(json.dumps(log_stats) + "\n")

        
        if eval_criterion == "loss":
                if early_stop.evaluate_decreasing_metric(val_metric=test_stats[eval_criterion]):
                    break
                if args.output_dir and test_stats[eval_criterion] <= best_stats[eval_criterion]:
                    misc.save_best_model(
                        args=args, model=model, model_without_ddp=model, optimizer=optimizer,
                        loss_scaler=loss_scaler, epoch=epoch, test_stats=test_stats, evaluation_criterion=eval_criterion)
                    best_stats[eval_criterion] = test_stats[eval_criterion]
        else:
            if early_stop.evaluate_increasing_metric(val_metric=test_stats[eval_criterion]):
                break
            if args.output_dir and test_stats[eval_criterion] >= best_stats[eval_criterion]:
                misc.save_best_model(
                    args=args, model=model, model_without_ddp=model, optimizer=optimizer,
                    loss_scaler=loss_scaler, epoch=epoch, test_stats=test_stats, evaluation_criterion=eval_criterion)
                best_stats[eval_criterion] = test_stats[eval_criterion]

    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    print('Training time {}'.format(total_time_str))


if __name__ == '__main__':
    args = get_args_parser()
    args = args.parse_args()
    if args.output_dir:
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    main(args)
