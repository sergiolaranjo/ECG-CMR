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
    loss_fn = torch.nn.MSELoss()
    accum_iter = args.accum_iter
    optimizer.zero_grad()

    if log_writer is not None:
        print('log_dir: {}'.format(log_writer.log_dir))

    for data_iter_step, batch in enumerate(metric_logger.log_every(data_loader, print_freq, header)):

        # we use a per iteration (instead of per epoch) lr scheduler
        if data_iter_step % accum_iter == 0:
            lr_sched.adjust_learning_rate(optimizer, data_iter_step / len(data_loader) + epoch, args)
        
        if args.cmr_mode == 'both':
            cmr = batch['cmr'].float().to(device, non_blocking=True)
            la_cmr = batch['la_cmr'].float().to(device, non_blocking=True)
        else:
            cmr = batch[args.cmr_mode].float().to(device, non_blocking=True)
        cond = batch['select_tar'].float().to(device, non_blocking=True)
        cha = batch['cha'].float().to(device)

        
        if args.downstream == 'classification':
            if args.dataset == 'mutimodal_dataset_zheyi_ECG' or args.dataset == 'mutimodal_dataset_MIMIC_ECG' or args.dataset == 'mutimodal_dataset_NEWECG' or args.dataset == 'mutimodal_dataset_MIMIC_NEWECG':
                cha = cha.float().to(device)
            
            else:
                cha = batch[args.classification_dis]
                cha = np.array([1 if x != '0' else 0 for x in cha])
                cha = torch.from_numpy(cha).float().to(device)

            cha = cha.unsqueeze(1)
            loss_fn = torch.nn.BCEWithLogitsLoss()

        with torch.cuda.amp.autocast():
            if args.cmr_mode == 'both':
                out = model(cmr, la_cmr, cond)
            else:
                out = model(cmr, cond)
            loss = loss_fn(out, cha)
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
                    args=None):
    model.eval()
    metric_logger = misc.MetricLogger(delimiter="  ")
    # metric_logger.add_meter('lr', misc.SmoothedValue(window_size=1, fmt='{value:.6f}'))
    header = 'Epoch: [{}]'.format(epoch)
    print_freq = 20
    loss_fn = torch.nn.MSELoss()
    accum_iter = args.accum_iter
    output = []
    label = []
    if log_writer is not None:
        print('log_dir: {}'.format(log_writer.log_dir))

    for data_iter_step, batch in enumerate(metric_logger.log_every(data_loader, print_freq, header)):

        if args.cmr_mode == 'both':
            cmr = batch['cmr'].float().to(device, non_blocking=True)
            la_cmr = batch['la_cmr'].float().to(device, non_blocking=True)
        else:
            cmr = batch[args.cmr_mode].float().to(device, non_blocking=True)
        cond = batch['select_tar'].float().to(device, non_blocking=True)
        cha = batch['cha'].float().to(device)

        
        if args.downstream == 'classification':
            if args.dataset == 'mutimodal_dataset_zheyi_ECG' or args.dataset == 'mutimodal_dataset_MIMIC_ECG' or args.dataset == 'mutimodal_dataset_NEWECG' or args.dataset == 'mutimodal_dataset_MIMIC_NEWECG':
                cha = cha.float().to(device)
            
            else:
                cha = batch[args.classification_dis]
                cha = np.array([1 if x != '0' else 0 for x in cha])
                cha = torch.from_numpy(cha).float().to(device)
                
            cha = cha.unsqueeze(1)
            loss_fn = torch.nn.BCEWithLogitsLoss()

        with torch.cuda.amp.autocast():
            if args.cmr_mode == 'both':
                headout = model(cmr, la_cmr, cond)
            else:
                headout = model(cmr, cond)
            loss = loss_fn(headout, cha)
            loss_value = loss.item()
            loss_name = args.downstream

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
    label = np.concatenate(label, axis=0)

    if args.downstream == 'classification':
        auc = roc_auc_score(label, output)
        metric_logger.update(auc=auc)
        log_writer.add_scalar('auc', auc, epoch)

    if args.downstream == 'regression':
        corr_list = []
        for i in range(82):
            # 创建一个子图
            # ax = fig.add_subplot(82, 1, i + 1)
            # 计算相关系数
            corr = np.corrcoef(output[:, i].flatten(), label[:, i].flatten())[0, 1]
            corr_list.append(corr)
            # 绘制散点图
            # ax.scatter(label[:, i].flatten(), output[:, i].flatten())
            # 在图上添加相关系数的文本
            # ax.text(0.1, 0.9, f'Correlation: {corr:.2f}', transform=ax.transAxes)
            # 设置子图的标题
            # ax.set_title(f'Feature {i + 1}')

        metric_logger.update(correlation=np.mean(corr_list))
        log_writer.add_scalar('correlation', np.mean(corr_list), epoch)
    

    metric_logger.synchronize_between_processes()
    print("validation stats:", metric_logger)
    return {k: meter.global_avg for k, meter in metric_logger.meters.items()}