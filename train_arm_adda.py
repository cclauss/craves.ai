from __future__ import print_function, absolute_import

import sys

import os
import argparse
import time
import matplotlib.pyplot as plt
import scipy
import json
import numpy as np
import cv2
from collections import deque

import torch
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim
import torchvision.datasets as datasets

from pose import Bar
from pose.utils.logger import Logger, savefig
from pose.utils.evaluation import accuracy, AverageMeter, final_preds, final_preds_bbox, get_preds, d3_acc
from pose.utils.misc import save_checkpoint, save_pred, adjust_learning_rate, command_converter
from pose.utils.osutils import mkdir_p, isfile, isdir, join
from pose.utils.imutils import batch_with_heatmap, sample_with_heatmap
from pose.utils.transforms import fliplr, flip_back, multi_scale_merge, align_back
from pose.utils.d2tod3 import d2tod3 #3-d pose estimation
import pose.models as models
import pose.datasets as datasets

sys.path.append('C:/Users/Yiming/Documents/GitHub/cycada_release') 

from cycada.models import Discriminator

def make_variable(tensor, volatile=False, requires_grad=True):
    if torch.cuda.is_available():
        tensor = tensor.cuda()
    if volatile:
        requires_grad = False
    return torch.autograd.Variable(tensor, volatile=volatile, requires_grad=requires_grad)

def supervised_loss(score, label, weights=None):
    loss_fn_ = torch.nn.NLLLoss(weight=weights, size_average=True, 
            ignore_index=255)
    loss = loss_fn_(torch.nn.functional.log_softmax(score, dim=1), label)
    return loss


discriminator = Discriminator(input_dim=256, output_dim=2, 
            pretrained=False, weights_init=None).cuda()

opt_dis = torch.optim.SGD(discriminator.parameters(), lr=0.0001, 
            momentum=0.9, weight_decay=0.0005)

accuracies_dom = deque(maxlen=100)


cur_iter = 0
max_iter = 1e4
exit_flag = False

model_names = sorted(name for name in models.__dict__
    if name.islower() and not name.startswith("__")
    and callable(models.__dict__[name]))



best_acc = 0


def main(args):
    num_datasets = len(args.data_dir) #number of datasets
    for item in [args.training_set_percentage, args.meta_dir, args.anno_type, args.ratio]:
        if len(item) == 1:
            for i in range(num_datasets-1):
                item.append(item[0])
        assert len(item) == num_datasets

    scales = [0.7, 0.85, 1, 1.3, 1.6]

    if args.meta_dir == '':
        args.meta_dir = args.data_dir #if not specified, assume meta info is stored in data dir.

    if args.evaluate: #creatng path for evaluation
        folders_to_create = ['preds', 'visualization']
        if args.save_heatmap:
            folders_to_create.append('heatmaps')
        for folder_name in folders_to_create:
            if not os.path.isdir(os.path.join(args.sample_img_dir, folder_name)):
                print('creating path: ' + os.path.join(args.sample_img_dir, folder_name))
                os.mkdir(os.path.join(args.sample_img_dir, folder_name))

    idx = range(args.num_classes)
    global best_acc

    cams = ['FusionCameraActor3']

    # create checkpoint dir
    if not isdir(args.checkpoint):
        mkdir_p(args.checkpoint)

    # create model
    print("==> creating model '{}', stacks={}, blocks={}".format(args.arch, args.stacks, args.blocks))
    model = models.__dict__['hg_feat'](num_stacks=args.stacks, num_blocks=args.blocks, num_classes=args.num_classes)
    

    model = torch.nn.DataParallel(model).cuda()

    # define loss function (criterion) and optimizer
    criterion = torch.nn.MSELoss(size_average=True).cuda()

    optimizer = torch.optim.RMSprop(model.parameters(), 
                                lr=args.lr,
                                momentum=args.momentum,
                                weight_decay=args.weight_decay)

    # optionally resume from a checkpoint
    title = 'arm-' + args.arch
    if args.resume:
        if isfile(args.resume):
            print("=> loading checkpoint '{}'".format(args.resume))
            checkpoint = torch.load(args.resume)
            args.start_epoch = checkpoint['epoch']
            best_acc = checkpoint['best_acc']
            model.load_state_dict(checkpoint['state_dict'])
            optimizer.load_state_dict(checkpoint['optimizer'])
            print("=> loaded checkpoint '{}' (epoch {})"
                  .format(args.resume, checkpoint['epoch']))
            logger = Logger(join(args.checkpoint, 'log.txt'), title=title, resume=True)
        else:
            print("=> no checkpoint found at '{}'".format(args.resume))
    else:        
        logger = Logger(join(args.checkpoint, 'log.txt'), title=title)
        logger.set_names(['Epoch', 'LR', 'Train Loss', 'Val Loss', 'Train Acc', 'Val Acc'])

    cudnn.benchmark = True
    print('    Total params: %.2fM' % (sum(p.numel() for p in model.parameters())/1000000.0))

    train_set_list = []
    val_set_list = []

    for i in range(num_datasets):
        train_set_list.append(datasets.Arm(args.data_dir[i], args.meta_dir[i], args.random_bg_dir, cams[0], args.anno_type[i],
        train=True, training_set_percentage = args.training_set_percentage[i], replace_bg=args.replace_bg))

        val_set_list.append(datasets.Arm(args.data_dir[i], args.meta_dir[i], args.random_bg_dir, cams[0], args.anno_type[i], 
        train=False, training_set_percentage = args.training_set_percentage[i], scales = scales, multi_scale=args.multi_scale, ignore_invis_pts=args.ignore_invis_pts))

    # Data loading code
    train_loader = torch.utils.data.DataLoader(
        datasets.Concat_w_class(datasets = train_set_list),
        batch_size=args.train_batch, shuffle=True,
        num_workers=args.workers, pin_memory=True)


    print("size of training set:{}".format(len(train_loader)))

    if args.multi_scale: #multi scale testing
        args.test_batch = args.test_batch*len(scales)
    
    val_loader = torch.utils.data.DataLoader(
        datasets.Concat(datasets = val_set_list),
        batch_size=args.test_batch, shuffle=False,
        num_workers=args.workers, pin_memory=True)

    print("size of validation set:{}".format(len(val_loader)))

    if args.evaluate:
        print('\nEvaluation only') 
        if not args.compute_3d:
            loss, acc = validate(val_loader, model, criterion, args.num_classes, idx, args.sample_img_dir,  args.meta_dir, args.anno_type,
                                        args.flip, args.evaluate, scales, args.multi_scale, args.save_heatmap)

        if args.compute_3d:

            preds = []
            gts = []
            hit, d3_pred, file_name_list = d2tod3(data_dir = args.sample_img_dir, meta_dir = args.meta_dir[0], cam_type = args.camera_type, pred_from_heatmap=False, em_test=False)

            #validate the 3d reconstruction accuracy
            
            with open(os.path.join(args.sample_img_dir, 'd3_pred.json'), 'r') as f:
                obj = json.load(f)
                hit, d3_pred, file_name_list = obj['hit'], obj['d3_pred'], obj['file_name_list']

            for file_name in file_name_list:
                preds.append(d3_pred[file_name]['preds']) #predicted x
                with open(os.path.join(args.data_dir[0], 'angles',file_name),'r') as f:
                    gts.append(json.load(f))

            print(d3_acc(preds, gts))
            
        return

    lr = args.lr
    for epoch in range(args.start_epoch, args.epochs):
        lr = adjust_learning_rate(optimizer, epoch, lr, args.schedule, args.gamma)
        print('\nEpoch: %d | LR: %.8f' % (epoch + 1, lr))

        # decay sigma
        if args.sigma_decay > 0:
            train_loader.dataset.sigma *=  args.sigma_decay
            val_loader.dataset.sigma *=  args.sigma_decay

        # train for one epoch
        train_loss, train_acc = train(train_loader, model, criterion, optimizer, idx, args.flip)

        # evaluate on validation set
        valid_loss, valid_acc = validate(val_loader, model, criterion, args.num_classes, idx, args.sample_img_dir, args.meta_dir, args.anno_type, args.flip, args.evaluate)

        # If concatenated dataset is used, re-random after each epoch
        train_loader.dataset.reset(), val_loader.dataset.reset()

        # append logger file
        logger.append([epoch + 1, lr, train_loss, valid_loss, train_acc, valid_acc])

        # remember best acc and save checkpoint
        is_best = valid_acc > best_acc
        best_acc = max(valid_acc, best_acc)
        save_checkpoint({
            'epoch': epoch + 1,
            'arch': args.arch,
            'state_dict': model.state_dict(),
            'best_acc': best_acc,
            'optimizer' : optimizer.state_dict(),
        }, is_best, checkpoint=args.checkpoint)

        # if exit_flag:
        #     break

    logger.close()
    logger.plot(['Train Acc', 'Val Acc'])
    savefig(os.path.join(args.checkpoint, 'log.eps'))


def train(train_loader, model, criterion, optimizer, idx, flip=True):
    # global cur_iter, max_iter, exit_flag

    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    acces = AverageMeter()

    # switch to train mode
    model.train()

    end = time.time()

    gt_win, pred_win = None, None
    bar = Bar('Processing', max=len(train_loader))
    for i, ((input_s, target_s, _), (input_t, _, _)) in enumerate(train_loader):
        # cur_iter += 1
        # if cur_iter >= max_iter:
        #     exit_flag = True
        #     break

        # measure data loading time
        data_time.update(time.time() - end)

        input_var_s = torch.autograd.Variable(input_s.cuda(), requires_grad=False)
        target_var_s = torch.autograd.Variable(target_s.cuda(), requires_grad=False)
        input_var_t = torch.autograd.Variable(input_t.cuda(), requires_grad=False)

        #update discriminator#
        

        opt_dis.zero_grad()
        optimizer.zero_grad()

        output_s, feat_s = model(input_var_s)
        dis_score_s = discriminator(feat_s.detach())

        output_t, feat_t = model(input_var_t)
        dis_score_t = discriminator(feat_t.detach())



        # output_s = model(input_var_s)[-1].detach()
        # output_t = model(input_var_t)[-1].detach()
        # dis_score_s = discriminator(output_s)
        # dis_score_t = discriminator(output_t)
        dis_pred_concat = torch.cat((dis_score_s, dis_score_t))

        #for visualzition
        # meanstd_file = './datasets/arm/mean.pth.tar'
        # meanstd = torch.load(meanstd_file)
        # mean = meanstd['mean']
        # inp = input_t[0]
        # score_map = output_t.data.cpu()
        # pred = score_map[0]
        # for t, m in zip(inp, mean):
        #     t.add_(m)
        # scipy.misc.imsave('C:/Users/Yiming/Desktop/tmp/0.jpg', sample_with_heatmap(inp, pred))

        batch_t,_,h,w = dis_score_t.size()
        batch_s,_,_,_ = dis_score_s.size()
        dis_label_concat = make_variable(
                torch.cat(
                    [torch.ones(batch_s,h,w).long(), 
                    torch.zeros(batch_t,h,w).long()]
                    ), requires_grad=False)

        # compute loss for discriminator
        loss_dis = supervised_loss(dis_pred_concat, dis_label_concat)
        (1.0 * loss_dis).backward()

        opt_dis.step()

        pred_dis = torch.squeeze(dis_pred_concat.max(1)[1])
        dom_acc = (pred_dis == dis_label_concat).float().mean().item() 

        accuracies_dom.append(dom_acc * 100.0)
        
        print('Updating D with adversarial loss, acc = {}, mean = {}'.format(dom_acc * 100.0, np.mean(accuracies_dom)))

        ###########################
        # Optimize Target Network #
        ###########################
        
        dom_acc_thresh = 65

        if np.mean(accuracies_dom) > dom_acc_thresh:

            print('Updating G with adversarial loss')

            optimizer.zero_grad()
            opt_dis.zero_grad()

            output_t, feat_t = model(input_var_t)
            dis_score_t = discriminator(feat_t)

            batch,_,h,w = dis_score_t.size()
            target_dom_fake_t = make_variable(torch.ones(batch,h,w).long(), 
                    requires_grad=False)

            loss_gan_t = supervised_loss(dis_score_t, target_dom_fake_t)
            (0.01 * loss_gan_t).backward()

            optimizer.step()

        print('Updating G using source supervised loss.')
        optimizer.zero_grad()
        opt_dis.zero_grad()

        output_s, feat_s = model(input_var_s)
        score_map = output_s[-1].data.cpu()

        loss = criterion(output_s[0], target_var_s)
        for j in range(1, len(output_s)):
            loss += criterion(output_s[j], target_var_s)

        acc = accuracy(score_map, target_s, idx, 0.2)     
        losses.update(loss.item(), input_s.size(0))
        acces.update(acc[0], input_s.size(0))

        loss.backward()
        optimizer.step()

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        # plot progress
        bar.suffix  = '({batch}/{size}) Data: {data:.6f}s | Batch: {bt:.3f}s | Total: {total:} | ETA: {eta:} | Loss: {loss:.4f} | Acc: {acc: .4f}'.format(
                    batch=i + 1,
                    size=len(train_loader),
                    data=data_time.val,
                    bt=batch_time.val,
                    total=bar.elapsed_td,
                    eta=bar.eta_td,
                    loss=losses.avg,
                    acc=acces.avg
                    )
        bar.next()

    bar.finish()
    return losses.avg, acces.avg


def validate(val_loader, model, criterion, num_classes, idx, sample_img_dir, meta_dir, anno_type, flip=True, evaluate = False,
        scales = [0.7, 0.8, 0.9, 1, 1.2, 1.4, 1.6], multi_scale = False, save_heatmap = False):
    
    anno_type = anno_type[0].lower()

    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    acces = AverageMeter()

    num_scales = len(scales)
    
    # switch to evaluate mode
    model.eval()

    meanstd_file = './datasets/arm/mean.pth.tar'
    meanstd = torch.load(meanstd_file)
    mean = meanstd['mean']

    gt_win, pred_win = None, None
    end = time.time()
    bar = Bar('Processing', max=len(val_loader))
    for i, (inputs, target, meta) in enumerate(val_loader):
        #print(inputs.shape)
        # measure data loading time
        data_time.update(time.time() - end)

        if anno_type != 'none':

            target = target.cuda(async=True)
            target_var = torch.autograd.Variable(target)

        input_var = torch.autograd.Variable(inputs.cuda())
        

        with torch.no_grad():
            # compute output
            output, _ = model(input_var)

            score_map = output[-1].data.cpu()
            if flip:
                flip_input_var = torch.autograd.Variable(
                        torch.from_numpy(fliplr(inputs.clone().numpy())).float().cuda(), 
                    )
                flip_output_var, _ = model(flip_input_var)
                flip_output = flip_back(flip_output_var[-1].data.cpu(), meta_dir = meta_dir[0])
                score_map += flip_output
                score_map /= 2

            if anno_type != 'none':

                loss = 0
                for o in output:
                    loss += criterion(o, target_var)
                acc = accuracy(score_map, target.cpu(), idx, 0.2)  

        if multi_scale:
            new_scales = []
            new_res = []
            new_score_map = []
            new_inp = []
            new_meta = []
            img_name = []
            confidence = []
            new_center = []

            num_imgs = score_map.size(0)//num_scales
            for n in range(num_imgs):
                score_map_merged, res, conf = multi_scale_merge(score_map[num_scales*n : num_scales*(n+1)].numpy(), meta['scale'][num_scales*n : num_scales*(n+1)])
                inp_merged, _, _ = multi_scale_merge(inputs[num_scales*n : num_scales*(n+1)].numpy(), meta['scale'][num_scales*n : num_scales*(n+1)])
                new_score_map.append(score_map_merged)
                new_scales.append(meta['scale'][num_scales*(n+1)-1]) 
                new_center.append(meta['center'][num_scales*n])
                new_res.append(res)
                new_inp.append(inp_merged)
                img_name.append(meta['img_name'][num_scales*n])
                confidence.append(conf)

            if len(new_score_map)>1:
                score_map = torch.tensor(np.stack(new_score_map)) #stack back to 4-dim
                inputs = torch.tensor(np.stack(new_inp))
            else:
                score_map = torch.tensor(np.expand_dims(new_score_map[0], axis = 0))
                inputs = torch.tensor(np.expand_dims(new_inp[0], axis = 0))


        else:
            img_name = []
            confidence = []
            for n in range(score_map.size(0)):
                img_name.append(meta['img_name'][n])
                confidence.append(np.amax(score_map[n].numpy(), axis = (1,2)).tolist())

        # generate predictions
        if multi_scale:
            preds = final_preds(score_map, new_center, new_scales, new_res[0])
        else:
            preds = final_preds(score_map, meta['center'], meta['scale'], [64, 64])

        for n in range(score_map.size(0)):
            if evaluate:
                with open(os.path.join(sample_img_dir,'preds',img_name[n]+'.json'),'w') as f:
                    obj = {'d2_key':preds[n].numpy().tolist(), 'score':confidence[n]}
                    json.dump(obj, f)

        if evaluate: 
            for n in range(score_map.size(0)):
                inp = inputs[n]
                pred = score_map[n]
                for t, m in zip(inp, mean):
                    t.add_(m)
                scipy.misc.imsave(os.path.join(sample_img_dir,'visualization', '{}.jpg'.format(img_name[n])), sample_with_heatmap(inp, pred))

                if save_heatmap:
                    score_map_original_size = align_back(score_map[n], meta['center'][n], meta['scale'][len(scales)*n - 1], meta['original_size'][n])
                    np.save(os.path.join(sample_img_dir, 'heatmaps', '{}.npy'.format(img_name[n])), score_map_original_size)

        if anno_type != 'none':

            # measure accuracy and record loss
            losses.update(loss.item(), inputs.size(0))
            acces.update(acc[0], inputs.size(0))

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        # plot progress
        bar.suffix  = '({batch}/{size}) Data: {data:.6f}s | Batch: {bt:.3f}s | Total: {total:} | ETA: {eta:} | Loss: {loss:.4f} | Acc: {acc: .4f}'.format(
                    batch=i + 1,
                    size=len(val_loader),
                    data=data_time.val,
                    bt=batch_time.avg,
                    total=bar.elapsed_td,
                    eta=bar.eta_td,
                    loss=losses.avg,
                    acc=acces.avg
                    )
        bar.next()

    bar.finish()

    if anno_type != 'none':
        return losses.avg, acces.avg
    else:
        return 0, 0

if __name__ == '__main__':
    def str2bool(v):
        if v.lower() in ('yes', 'true', 't', 'y', '1'):
            return True
        elif v.lower() in ('no', 'false', 'f', 'n', '0'):
            return False
        else:
            raise argparse.ArgumentTypeError('Unsupported value encountered.')

    parser = argparse.ArgumentParser(description='PyTorch ImageNet Training')
    # Model structure
    parser.add_argument('--arch', '-a', metavar='ARCH', default='hg',
                        choices=model_names,
                        help='model architecture: ' +
                            ' | '.join(model_names) +
                            ' (default: resnet18)')
    parser.add_argument('-s', '--stacks', default=2, type=int, metavar='N',
                        help='Number of hourglasses to stack')
    parser.add_argument('--features', default=256, type=int, metavar='N',
                        help='Number of features in the hourglass')
    parser.add_argument('-b', '--blocks', default=1, type=int, metavar='N',
                        help='Number of residual modules at each location in the hourglass')
    parser.add_argument('--num-classes', default=17, type=int, metavar='N',
                        help='Number of keypoints')
    # Training strategy
    parser.add_argument('-j', '--workers', default=4, type=int, metavar='N',
                        help='number of data loading workers (default: 4)')
    parser.add_argument('--epochs', default=30, type=int, metavar='N',
                        help='number of total epochs to run')
    parser.add_argument('--start-epoch', default=0, type=int, metavar='N',
                        help='manual epoch number (useful on restarts)')
    parser.add_argument('--train-batch', default=6, type=int, metavar='N',
                        help='train batchsize')
    parser.add_argument('--test-batch', default=6, type=int, metavar='N',
                        help='test batchsize')
    parser.add_argument('--lr', '--learning-rate', default=2.5e-4, type=float,
                        metavar='LR', help='initial learning rate')
    parser.add_argument('--momentum', default=0, type=float, metavar='M',
                        help='momentum')
    parser.add_argument('--weight-decay', '--wd', default=0, type=float,
                        metavar='W', help='weight decay (default: 0)')
    parser.add_argument('--schedule', type=int, nargs='+', default=[20, ],
                        help='Decrease learning rate at these epochs.')
    parser.add_argument('--gamma', type=float, default=0.1,
                        help='LR is multiplied by gamma on schedule.')
    parser.add_argument('--training-set-percentage', nargs = '+', type=float, default=[0.9, ],
                        help='training set percentage')              
    # Data processing
    parser.add_argument('-f', '--flip', dest='flip', action='store_true',
                        help='flip the input during validation')
    parser.add_argument('--sigma', type=float, default=1,
                        help='Groundtruth Gaussian sigma.')
    parser.add_argument('--sigma-decay', type=float, default=0,
                        help='Sigma decay rate for each epoch.')
    parser.add_argument('--label-type', metavar='LABELTYPE', default='Gaussian',
                        choices=['Gaussian', 'Cauchy'],
                        help='Labelmap dist type: (default=Gaussian)')
    parser.add_argument('--use-bbox', action='store_true',
                        help='use bbox or center-scale mode')
    parser.add_argument('--whole-img', action='store_true',
                        help='does not provide bounding box. Feed whole image into network')
    parser.add_argument('--multi-scale', action='store_true',
                        help='do multi-scale testing')
    parser.add_argument('--replace-bg', action='store_true',
                        help='background repalcement when doing finetune')
    parser.add_argument('--ignore-invis-pts', action='store_true',
                        help='ignore the invisible points when testing on youtube videos')
                                 
    # Miscs
    parser.add_argument('-c', '--checkpoint', default='checkpoint', type=str, metavar='PATH',
                        help='path to save checkpoint (default: checkpoint)')
    parser.add_argument('--data-dir', type=str, nargs='+' ,metavar='PATH', help='path where data is saved')
    parser.add_argument('--meta-dir', type=str, nargs='+' ,metavar='PATH', help='path where meta data is saved', default = './data/meta/17_vertex')
    parser.add_argument('--sample-img-dir', type=str, metavar='PATH', help='path for saving sample images for visualization')
    parser.add_argument('--random-bg-dir', default = '', type=str, metavar='PATH', help='path from which random background for finetuneing is sampled')
    parser.add_argument('--resume', default='', type=str, metavar='PATH',
                        help='path to latest checkpoint (default: none)')
    parser.add_argument('-e', '--evaluate', dest='evaluate', action='store_true',
                        help='evaluate model on validation set')
    parser.add_argument('--anno-type', type=str, nargs='+', help='annotation type of each sub-dataset; should be eith 3D, 2D or None')
    parser.add_argument('--ratio', type=float, nargs='+', default = [1], 
                        help='Ratio for dataset concatination')
    parser.add_argument('--compute-3d', action='store_true',
                        help='compute 3d angles during validation')
    parser.add_argument('--camera-type', type = str, default = 'video',
                        help='compute 3d angles during validation')
    parser.add_argument('--save-heatmap', action='store_true',
                        help='save heatmap as .npy file')

    main(parser.parse_args())