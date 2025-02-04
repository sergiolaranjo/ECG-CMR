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
import math
import sys
from typing import Iterable
import matplotlib.pyplot as plt
import torch
import numpy as np
import util.misc as misc
import util.lr_sched as lr_sched
import os
def softmax(x):
    # 减去最大值以稳定数值计算，防止溢出
    e_x = np.exp(x - np.max(x, axis=-1, keepdims=True))
    return e_x / np.sum(e_x, axis=-1, keepdims=True)

def train_one_epoch(model: torch.nn.Module,
                    data_loader: Iterable, optimizer: torch.optim.Optimizer,
                    device: torch.device, epoch: int, loss_scaler,
                    log_writer=None,
                    args=None):
    model.train()
    metric_logger = misc.MetricLogger(delimiter="  ")
    metric_logger.add_meter('lr', misc.SmoothedValue(window_size=1, fmt='{value:.6f}'))
    header = 'Epoch: [{}]'.format(epoch)
    print_freq = 20
    loss_fn = torch.nn.CrossEntropyLoss()
    accum_iter = args.accum_iter
    optimizer.zero_grad()

    if log_writer is not None:
        print('log_dir: {}'.format(log_writer.log_dir))

    for data_iter_step, batch in enumerate(metric_logger.log_every(data_loader, print_freq, header)):

        # we use a per iteration (instead of per epoch) lr scheduler
        if data_iter_step % accum_iter == 0:
            lr_sched.adjust_learning_rate(optimizer, data_iter_step / len(data_loader) + epoch, args)
        
        ecg = batch['ecg'].unsqueeze(1).float().to(device, non_blocking=True)
        cond = batch['select_tar'].float().to(device, non_blocking=True)
        cha = batch['cha'].long().to(device)
        
        
        
        with torch.cuda.amp.autocast():
            ecg = ecg.float()
            _, headout = model(ecg, cond)
            loss = loss_fn(headout, cha)
            loss_value = loss.item()
            loss_name = args.downstream

        if not math.isfinite(loss_value):
            print("Loss is {}, stopping training".format(loss_value))
            sys.exit(1)

        loss /= accum_iter
        loss_scaler(loss, optimizer, parameters=model.parameters(),
                    update_grad=(data_iter_step + 1) % accum_iter == 0)
        if (data_iter_step + 1) % accum_iter == 0:
            optimizer.zero_grad()

        torch.cuda.synchronize()

        metric_logger.update(loss=loss_value)
        lr = optimizer.param_groups[0]["lr"]
        metric_logger.update(lr=lr)

        loss_value_reduce = misc.all_reduce_mean(loss_value)
        if log_writer is not None and (data_iter_step + 1) % accum_iter == 0:
            """ We use epoch_1000x as the x-axis in tensorboard.
            This calibrates different curves when batch size changes.
            """
            epoch_1000x = int((data_iter_step / len(data_loader) + epoch) * 1000)
            log_writer.add_scalar('train_loss', loss_value_reduce, epoch_1000x)
            log_writer.add_scalar('lr', lr, epoch_1000x)


    # gather the stats from all processes
    metric_logger.synchronize_between_processes()
    print("Averaged stats:", metric_logger)
    return {k: meter.global_avg for k, meter in metric_logger.meters.items()}



from sklearn.metrics import roc_auc_score
@torch.no_grad()
def evaluate(model: torch.nn.Module,
                    data_loader: Iterable, optimizer: torch.optim.Optimizer,
                    device: torch.device, epoch: int, loss_scaler,
                    log_writer=None,
                    args=None,
                    best_stats=None,
                    eval_criterion=None):
    model.eval()
    metric_logger = misc.MetricLogger(delimiter="  ")
    # metric_logger.add_meter('lr', misc.SmoothedValue(window_size=1, fmt='{value:.6f}'))
    header = 'Epoch: [{}]'.format(epoch)
    print_freq = 20
    loss_fn = torch.nn.CrossEntropyLoss()
    accum_iter = args.accum_iter
    output = []
    label = []
    if log_writer is not None:
        print('log_dir: {}'.format(log_writer.log_dir))

    for data_iter_step, batch in enumerate(metric_logger.log_every(data_loader, print_freq, header)):

       
        
        ecg = batch['ecg'].unsqueeze(1).float().to(device, non_blocking=True)
        cond = batch['select_tar'].float().to(device, non_blocking=True)
        cha = batch['cha'].long().to(device)


        with torch.cuda.amp.autocast():
            _, headout = model(ecg, cond)
            loss = loss_fn(headout, cha)
            loss_value = loss.item()

        label.append(cha.cpu().detach().numpy())
        out = headout.cpu().detach().numpy()
        out = out.reshape(-1, out.shape[-1])  # reshape the output
        output.append(out)



        if not math.isfinite(loss_value):
            print("Loss is {}, stopping valing".format(loss_value))
            sys.exit(1)

        metric_logger.update(loss=loss_value)
        

        loss_value_reduce = misc.all_reduce_mean(loss_value)
        if log_writer is not None and (data_iter_step + 1) % accum_iter == 0:
            """ We use epoch_1000x as the x-axis in tensorboard.
            This calibrates different curves when batch size changes.
            """
            epoch_1000x = int((data_iter_step / len(data_loader) + epoch) * 1000)
            log_writer.add_scalar('test_loss', loss_value_reduce, epoch_1000x)

    output = np.concatenate(output, axis=0)
    label = np.concatenate(label, axis=0).astype(int)
    
    output = torch.nn.functional.softmax(torch.tensor(output).float(), dim=-1).numpy()
    

    auc = roc_auc_score(label, output,multi_class='ovr')
    metric_logger.update(auc=auc)
    log_writer.add_scalar('auc', auc, epoch)

    metric_logger.synchronize_between_processes()
    print("validation stats:", metric_logger)
    test_stats = {k: meter.global_avg for k, meter in metric_logger.meters.items()}
    
    return test_stats