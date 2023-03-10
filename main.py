from __future__ import print_function

import os
import time
import random
import warnings
import numpy as np
import pandas as pd
import socket
import datetime
import pathlib

import models
import argparse
from utils.arguments import set_arguments, check_arguments

args = set_arguments()

from utils import data_loader, initialize, logging, misc, tensorboard, eval
from utils.misc import ForkedPdb
from utils.schedule_train import set_optimizer, set_scheduler

from utils.preproc_bi_real_net import CrossEntropyLabelSmooth	# for Bi-real-net
from models.psum_modules import set_BitSerial_log, set_Noise_injection, unset_BitSerial_log

warnings.simplefilter("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", "Corrupt EXIF data", UserWarning)
warnings.filterwarnings("ignore", "Possibly corrupt EXIF data", UserWarning)
warnings.filterwarnings("ignore", "Metadata Warning", UserWarning)

# import torch modules
import torch
import torch.backends.cudnn as cudnn
import torch.distributed as dist
import torch.multiprocessing as mp

assert torch.cuda.is_available(), "check if cuda is avaliable"

torch.autograd.set_detect_anomaly(True)

def PickUnusedPort():
    
    '''Picks unused port in the current node'''
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('localhost', 0))
    addr, port = s.getsockname()
    s.close()

    return port


def SetRandomSeed(seed=None):
    '''Sets random seed for all torch methods'''
    if seed is None:
        seed = random.randint(1, 10000)
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)

    return seed


def main():
    check_arguments(args)

    # it is required that the number of gpu's visible by each node should be same.
    # we don't know if setting them using gpu-id work yet.
    ngpus_per_node = torch.cuda.device_count()

    # https://github.com/pytorch/examples/blob/master/imagenet/main.py
    # nr == initial rank, nodes == initial world_size in above code.
    # In the example above (1 node, multigpu), world_size: 1, distributed: True,
    # multiprocessing_distributed: True,
    args.nr = 0
    args.nodes = 1
    args.world_size = 1

    if args.distributed:
        # world size equals the total available gpus for DDP, fixed to 1 in DP.
        args.world_size = ngpus_per_node * args.nodes
        args.dist_backend = "nccl"
        args.dist_url = f"tcp://127.0.0.1:{PickUnusedPort()}"
        print(f"Binding to processes using : {args.dist_url}")


    if args.checkpoint is None:
        if args.evaluate:
            if args.arraySize > 0:
                if args.is_noise:
                    if args.noise_train:
                        if args.wsymmetric:
                            prefix = os.path.join("checkpoints", args.dataset, "eval", args.arch, args.mapping_mode, "{}_c:{}".format(str(args.arraySize), args.cbits),"a:{}_w:{}".format(args.abits,args.wbits), "class_split_per_{}".format(args.per_class), 'noise_std_{}'.format(args.noise_type), "noise_{}_ratio_{}_train_{}".format(args.noise_param, args.ratio, args.trained_noise), "weight_sym")
                        else: 
                            prefix = os.path.join("checkpoints", args.dataset, "eval", args.arch, args.mapping_mode, "{}_c:{}".format(str(args.arraySize), args.cbits),"a:{}_w:{}".format(args.abits,args.wbits), "class_split_per_{}".format(args.per_class), 'noise_std_{}'.format(args.noise_type), "noise_{}_ratio_{}_train_{}".format(args.noise_param, args.ratio, args.trained_noise))
                    else:
                        if args.wsymmetric:
                            prefix = os.path.join("checkpoints", args.dataset, "eval", args.arch, args.mapping_mode, "{}_c:{}".format(str(args.arraySize), args.cbits),"a:{}_w:{}".format(args.abits,args.wbits), "class_split_per_{}".format(args.per_class), 'noise_std_{}'.format(args.noise_type), "noise_{}_ratio_{}".format(args.noise_param, args.ratio), "weight_sym")
                        else: 
                            prefix = os.path.join("checkpoints", args.dataset, "eval", args.arch, args.mapping_mode, "{}_c:{}".format(str(args.arraySize), args.cbits),"a:{}_w:{}".format(args.abits,args.wbits), "class_split_per_{}".format(args.per_class), 'noise_std_{}'.format(args.noise_type), "noise_{}_ratio_{}".format(args.noise_param, args.ratio))
                else:
                    if args.mapping_mode == "2T2R":
                        if args.wsymmetric:
                            prefix = os.path.join("checkpoints", args.dataset, "eval", args.arch, args.mapping_mode, "{}_c:{}".format(str(args.arraySize), args.cbits),"a:{}_w:{}".format(args.abits,args.wbits), "class_split_per_{}".format(args.per_class), "weight_sym")
                        else: 
                            prefix = os.path.join("checkpoints", args.dataset, "eval", args.arch, args.mapping_mode, "{}_c:{}".format(str(args.arraySize), args.cbits),"a:{}_w:{}".format(args.abits,args.wbits), "class_split_per_{}".format(args.per_class))
                    else:
                        prefix = os.path.join("checkpoints", args.dataset, "eval", args.arch, args.mapping_mode, "{}_c:{}".format(str(args.arraySize), args.cbits),"a:{}_w:{}".format(args.abits,args.wbits), "class_split_per_{}".format(args.per_class))
            else:
                prefix = os.path.join("checkpoints", args.dataset, "eval", args.arch, "a:{}_w:{}".format(args.abits,args.wbits), "search_img:{}".format(args.search_img))
        else:
            if args.arraySize > 0:
                if args.is_noise:
                    prefix = os.path.join("checkpoints", args.dataset, args.arch, args.mapping_mode, "{}_c:{}".format(str(args.arraySize), args.cbits), "a:{}_w:{}".format(args.abits,args.wbits), "trained_noise_{}_ratio_{}".format(args.trained_noise, args.ratio))
                else:
                    prefix = os.path.join("checkpoints", args.dataset, args.arch, args.mapping_mode, "{}_c:{}".format(str(args.arraySize), args.cbits), "a:{}_w:{}".format(args.abits,args.wbits))
            else:
                if not args.fix_bit:
                    prefix = os.path.join("checkpoints", args.dataset, args.arch, "mpq_noise")
                else:
                    prefix = os.path.join("checkpoints", args.dataset, args.arch, "fpq_noise_{}".format(args.fix_bit))


        
        if args.local is None:
            prefix = prefix
        else:
            prefix_bak = prefix
            prefix = os.path.join(args.local, prefix_bak)

        if args.psum_comp:
            args.checkpoint = os.path.join(prefix, "log_bitserial_info")
            if not os.path.exists(args.checkpoint):
                os.makedirs(args.checkpoint)
                os.makedirs(os.path.join(args.checkpoint, 'hist'))
            else:
                if args.log_file:
                    print(f"remove folder {args.checkpoint}")
                    os.system(f'rm -rf {args.checkpoint}')
                    print(f"create new folder {args.checkpoint}")
                    os.makedirs(args.checkpoint)
                    os.makedirs(os.path.join(args.checkpoint, 'hist'))
        else:
            args.checkpoint = misc.mkdir_now(prefix)
    else: 
        os.makedirs(args.checkpoint, exist_ok=True)
    print(f"==> Save everything in \n {args.checkpoint}")

    misc.lndir_p(args.checkpoint, args.link)

    # Use torch.multiprocessing.spawn to launch distributed processes: the
    # main_worker process function
    if args.distributed:
        mp.spawn(main_worker, nprocs=ngpus_per_node, args=(ngpus_per_node, args), join=True)
    else:
        main_worker(0, ngpus_per_node, args)

     # move to checkpoint file
    if args.local:
        dest_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), prefix_bak)
        misc.copy_folder(args.checkpoint, dest_path)


# gpu numbers vary from 0 to (ngpus_per_node - 1)
def main_worker(gpu, ngpus_per_node, args):

    # initialize parameters for results
    top1 = {'train': 0, 'valid': 0, 'test': 0, 'best_valid': 0, 'test_at_best_valid_top1': 0, 'best_test': 0}
    top5 = {'train': 0, 'valid': 0, 'test': 0, 'best_valid': 0, 'test_at_best_valid_top1': 0, 'best_test': 0}
    loss = {'train': None, 'valid': None, 'test': None}

    start_epoch = args.start_epoch  # start from epoch 0 or last checkpoint epoch

    # Sets current device ordinal.
    args.gpu = gpu
    torch.cuda.set_device(args.gpu)

    # TODO (VINN): when implementing multi-node DDP, check these values.
    # Check https://github.com/pytorch/examples/blob/master/imagenet/main.py
    if args.distributed:
        # for nodes = 2, ngpus_per_node = 4,
        # rank = 0 ~ 3 for gpus in nr 0, 4 ~ 7 for gpus in nr 1
        args.rank = args.nr * ngpus_per_node + gpu
        dist.init_process_group(backend=args.dist_backend, init_method=args.dist_url,
                                world_size=args.world_size, rank=args.rank)
    else:
        args.rank = 0

    args.manualSeed = SetRandomSeed(seed=args.manualSeed)
    writer = tensorboard.Tensorboard_writer(args.checkpoint, args.tensorboard, args.rank)

    # Set ddp arguments
    if args.distributed:
        args.train_batch = int(args.train_batch / ngpus_per_node)
        args.test_batch = int(args.test_batch / ngpus_per_node)
        args.workers = int((args.workers + ngpus_per_node - 1) / ngpus_per_node)
        print(f"gpu: {args.gpu}, current_device: {torch.cuda.current_device()}, "
              f"train_batch_per_gpu: {args.train_batch}, workers_per_gpu: {args.workers}")
    else:
        print(f"gpu: {args.gpu_id}, current_device: {torch.cuda.current_device()}, "
              f"train_batch_total: {args.train_batch}, workers_total: {args.workers}")


    # Set data loader
    cudnn.benchmark = True
    #cudnn.deterministic = True
    train_loader, train_sampler, valid_loader, test_loader = data_loader.set_data_loader(args)

    # Load teacher model if required
    if args.teacher is not None:
        print("==> Loading teacher model...")
        assert os.path.exists(args.teacher), 'Error: no teacher model found!'
        state = torch.load(args.teacher, map_location=lambda storage, loc: storage.cuda(args.gpu))
        teacher = models.__dict__[state['args_dict']['arch']](**state['args_dict'])
#        for p in teacher.parameters():
#            p.requires_grad = False
    else:
        teacher = None

    # Create model.
    assert torch.backends.cudnn.enabled, 'Amp requires cudnn backend to be enabled.'
    args_dict = vars(args)
    model = models.__dict__[args.arch](**args_dict)

        
    # initialize weights
    modules_to_init = ['Conv2d', 'BinConv', 'Linear', 'BinLinear', \
            'QConv', 'QLinear', 'QuantConv', 'QuantLinear', \
            'Q_Conv2d', 'Q_Linear', \
            'PsumQConv', 'PsumQLinear']
    bn_modules_to_init = ['BatchNorm1d', 'BatchNorm2d']
    for m in model.modules():
        if type(m).__name__ in modules_to_init:
            initialize.init_weight(m, method=args.init_method, dist=args.init_dist, mode=args.init_fan)
            # print('Layer {} has been initialized.'.format(type(m).__name__))
        if type(m).__name__ in bn_modules_to_init:
            if args.bn_bias != 0.0:    
                # print(f"Initializing BN bias to {args.bn_bias}")
                torch.nn.init.constant_(m.bias, args.bn_bias)
                #print('Layer {} has been initialized.'.format(type(m).__name__))

    # define loss function (criterion) and optimizer
    criterion = torch.nn.CrossEntropyLoss()

    # We want to make sure that we apply weight decay only to weights.
    all_parameters = model.parameters()
    weight_parameters = []
    for pname, p in model.named_parameters():
        #print(f"name: {pname}, dimension: {p.ndimension()}")
        if p.ndimension() != 1 and 'weight' in pname:
            weight_parameters.append(p)
    weight_parameters_id = list(map(id, weight_parameters))
    other_parameters = list(filter(lambda p: id(p) not in weight_parameters_id, all_parameters))

    optimizer = set_optimizer(model, other_parameters, weight_parameters, args)
    
    # Initialize scaler if amp is used.
    if args.amp:
        scaler = torch.cuda.amp.GradScaler(enabled=args.amp)
    else:
        scaler = None


    # Initialize scheduler. This is done after the amp initialize to avoid warning.
    # Refer to Issues: https://discuss.pytorch.org/t/cyclic-learning-rate-how-to-use/53796
    scheduler, after_scheduler = set_scheduler(optimizer, args)

    # Wrap the model with DDP
    if args.distributed:
        print(f"student gpu-id: {args.gpu}")
        model = torch.nn.parallel.DistributedDataParallel(
            model.to("cuda"), device_ids=[args.gpu], output_device=args.gpu, find_unused_parameters=True)
    else:
        model = torch.nn.DataParallel(model).to("cuda")


    # Wrap the teacher model with DDP if exist
    if teacher is not None:
        if args.distributed:
            print(f"teacher gpu-id: {args.gpu}")
            teacher = torch.nn.parallel.DistributedDataParallel(
                teacher.to("cuda"), device_ids=[args.gpu], output_device=args.gpu)
        else:
            teacher = torch.nn.DataParallel(teacher).to("cuda")
        teacher.load_state_dict(state['state_dict'])

    # Print model information
    if args.rank == 0:
        print('---------- args -----------')
        print(args)
        print('---------- model ----------')
        print(model)

        ## TODO (VINN): We want to take quantization into account!
        total_parameters = sum(p.numel() for p in model.parameters())/1000000.0
        print(f"    Total params: {total_parameters:.2f}M")


    # Load pre-trained model if enabled
    if args.pretrained:
        if args.rank == 0:
            print('==> Get pre-trained model..')
        assert os.path.exists(args.pretrained), 'Error: no pre-trained model found!'

        load_dict = torch.load(args.pretrained, map_location=lambda storage, loc: storage.cuda(args.gpu))['state_dict']
        model_dict = model.state_dict()
        model_keys = model_dict.keys()
        for name, param in load_dict.items():
            if name in model_keys:
                model_dict[name] = param
        model.load_state_dict(model_dict)

    # Overwrite arguments from reading checkpoint's arg_dict
    # Load from checkpoint if resume is enabled.
    if args.resume:
        if args.rank == 0:
            print('==> Overwriting arguments ... (Resuming)')
        assert os.path.isdir(args.resume), 'Error: no checkpoint directory found!'
        checkpoint = torch.load(os.path.join(args.resume, 'checkpoint.pth.tar'),
                                map_location=lambda storage, loc: storage.cuda(args.gpu))
        # overwrite, except for special arguments.
        args_dict = checkpoint['args_dict']
        assert args_dict['gpu_id'].count(",") == args.gpu_id.count(","), \
                'Do not change the number of GPU used when resuming!'
        args_dict['gpu_id'] = args.gpu_id
        args_dict['workers'] = args.workers
        args_dict['resume'] = args.resume
        args_dict['gpu'] = args.gpu
        args_dict['rank'] = args.rank
        args_dict['nr'] = args.nr
        args_dict['checkpoint'] = args.checkpoint
        args = argparse.Namespace(**args_dict)

        if args.rank == 0:
            print('------ resuming with args -------')
            print(args)
            print('==> Resuming from checkpoint..')

        # Load checkpoint.
        top1 = checkpoint['top1']
        top5 = checkpoint['top5']
        start_epoch = checkpoint['epoch']
        scheduler.load_state_dict(checkpoint['scheduler'])
        after_scheduler.load_state_dict(checkpoint['after_scheduler'])
        model.load_state_dict(checkpoint['state_dict'])
        optimizer.load_state_dict(checkpoint['optimizer'])
        if args.amp:
            scaler.load_state_dict(checkpoint['amp'])

    if args.rank == 0:
        # Save model structure and arguments to model.txt inside the checkpoint.
        with open(f"{args.checkpoint}/model.txt", 'w') as f:
            f.write(str(model) + "\n")
            for key, value in args_dict.items():
                f.write(f"{key:<20}: {value}\n")


    title = args.dataset + '-' + args.arch
    logger = logging.Logger(os.path.join(args.checkpoint, 'log.txt'), title=title)

    logger.set_names([
        'Learning Rate', 'Train Loss', 'Valid Loss', 'Test Loss', 'Train Top1',
        'Train Top5', 'Valid Top1', 'Valid Top5', 'Test Top1', 'Test Top5'])

    graph_path = None
    if args.rank % ngpus_per_node == 0:
        # Resume and initializing logger
        if args.evaluate:
            report_path = os.path.join(str(pathlib.Path().resolve()), ((args.checkpoint.replace('checkpoints', 'report')).replace('eval/', '')).replace('/log_bitserial_info', ''))
            graph_path = os.path.join(str(pathlib.Path().resolve()), 'graph', args.dataset, f'Psum_{args.arch}', args.mapping_mode, args.psum_mode, 'class_{}'.format(args.per_class))
            os.makedirs(report_path, exist_ok=True)
            report_file = os.path.join(report_path, 'model_report.pkl')


    start_time=time.time()
    log_time = start_time
    if args.is_noise:
        set_Noise_injection(model, noise_param=args.noise_param, ratio=args.ratio)
    
    if args.psum_comp:

        set_BitSerial_log(model, checkpoint=args.checkpoint, log_file=args.log_file,\
            pbits=args.pbits, pclipmode=args.pclipmode, pclip=args.pclip, psigma=args.psigma, graph_path=graph_path)

        if args.log_file:
            if args.class_split:
                eval.log_test(valid_loader, model, args)
            else:
                eval.log_test(train_loader, model, args)

            unset_BitSerial_log(model)
            log_time = time.time()

    if args.evaluate:

        if args.rank == 0:
            print('\nEvaluation only')
        if not args.class_split:
            if valid_loader is not None:
                loss['valid'], top1['valid'], top5['valid'] = eval.test(valid_loader, model, criterion, 0, args)
                if args.rank == 0:
                    print(f"Valid loss: {loss['valid']:<10.6f} Valid top1: {top1['valid']:<7.4f} Valid top5: {top5['valid']:<7.4f}")

        if test_loader is not None:
            loss['test'], top1['test'], top5['test'] = eval.test(test_loader, model, criterion, 0, args)
            if args.rank == 0:
                print(f"Test loss: {loss['test']:<10.6f} Test top1: {top1['test']:<7.4f} Test top5: {top5['test']:<7.4f}")

        # reset DALI iterators
        if args.dali:
            if valid_loader is not None:
                valid_loader.reset()
            if test_loader is not None:
                test_loader.reset()

        if args.rank == 0:
            end=time.time()
            print('\nPsum Parameter Search Total time : {total_time}s'.format(
                        total_time=end-start_time))

            # log evaluation result
            if os.path.isfile(report_file):
                if args.testlog_reset:
                    df = pd.DataFrame()
                else:
                    df = pd.read_pickle(report_file)
            else:
                df = pd.DataFrame()

        df = df.append({
            "dataset":          args.dataset,
            "Network":          args.arch,
            "Mapping_mode":     args.mapping_mode,
            "arraySize":        args.arraySize,
            'cell bits':        args.cbits,
            "per_class":        args.per_class,
            "pbits":            args.pbits,
            "psum_mode":        args.psum_mode,
            "pclipmode":        args.pclipmode,
            "pclip":            args.pclip,
            "noise":            args.is_noise,
            "noise_paramiation":  args.noise_param,
            "Test Top1":        top1['test'],
            "Test Top5":        top5['test'],
            "Log time":         log_time-start_time,
            "Total Time":       end-start_time
            }, ignore_index=True)
        df.to_pickle(report_file)
        df.to_csv(report_path+'/accuracy_report.txt', sep = '\t', index = False)

        return

    # Evaluate the performance of teacher model
    if args.teacher is not None:
        if args.rank == 0:
            print("\nPerformance of teacher model")

        if valid_loader is not None:
            loss['valid'], top1['valid'], top5['valid'] = eval.test(valid_loader, teacher, criterion, 0, args)
            if args.rank == 0:
                print(f"Valid loss: {loss['valid']:<10.6f} Valid top1: {top1['valid']:<7.4f} Valid top5: {top5['valid']:<7.4f}")

        if test_loader is not None:
            loss['test'], top1['test'], top5['test'] = eval.test(test_loader, teacher, criterion, 0, args)
            if args.rank == 0:
                print(f"Test loss: {loss['test']:<10.6f} Test top1: {top1['test']:<7.4f} Test top5: {top5['test']:<7.4f}")

        # reset DALI iterators
        if args.dali:
            valid_loader.reset()

    # Train and val
    start_time=time.time()
    for epoch in range(start_epoch, args.epochs):
        # np.random.seed(args.manualSeed+epoch)

        if not args.dali and args.distributed:
            train_sampler.set_epoch(epoch)

        # TODO (VINN): if anyone can find a better way to caculate lr with glorot scaling, please do.
        # The way to do it without glr is scheduler.get_lr()
        current_lr = args.lr * optimizer.param_groups[0]['lr'] / optimizer.param_groups[0]['initial_lr']
        if args.rank == 0:
            print(f"\nEpoch: [{epoch + 1} | {args.epochs}] LR: {current_lr:.3e}")

        # log parameters & buffers on tensorboard
        writer.log_param(model, epoch)

        loss['train'], top1['train'], top5['train'] = eval.train(
            train_loader, model, teacher, criterion,
            optimizer, scheduler, scaler, epoch, writer, args)

        # log train loss and accuracy on tensorboard
        writer.log_scalars('train', {'loss': loss['train'], 'top1': top1['train'], 'top5': top5['train']}, epoch)
        
        # steo firward the scheduler.
        scheduler.step()

        # Logging and saving
        if valid_loader is not None:
            loss['valid'], top1['valid'], top5['valid'] = eval.test(valid_loader, model, criterion, epoch, args)
        if test_loader is not None:
            loss['test'], top1['test'], top5['test'] = eval.test(test_loader, model, criterion, epoch, args)

        if valid_loader is not None:
            is_best_valid_top1 = top1['valid'] > top1['best_valid']
            top1['best_valid'] = max(top1['valid'], top1['best_valid'])
            top5['best_valid'] = max(top5['valid'], top5['best_valid'])
            top1['test_at_best_valid_top1'] = top1['test'] if is_best_valid_top1 else top1['test_at_best_valid_top1']
            top5['test_at_best_valid_top1'] = top5['test'] if is_best_valid_top1 else top5['test_at_best_valid_top1']
        elif test_loader is not None:
            is_best_test_top1 = top1['test'] > top1['best_test']
            top1['best_test'] = max(top1['test'], top1['best_test'])
            top5['best_test'] = max(top5['test'], top5['best_test'])


        if args.rank % ngpus_per_node == 0:
            # Logs are saved in each node.
            logger.append([
                current_lr, loss['train'], loss['valid'], loss['test'],
                top1['train'], top5['train'], top1['valid'], top5['valid'], top1['test'], top5['test']])
            
            if valid_loader is not None:
                logging.save_checkpoint(
                    {
                        'epoch': epoch + 1,
                        'state_dict':               model.state_dict(),
                        'top1':                     top1,
                        'top5':                     top5,
                        'optimizer':                optimizer.state_dict(),
                        'amp':                      scaler.state_dict() if args.amp else None,
                        'args_dict':                args_dict,
                        'scheduler':                scheduler.state_dict(),
                        'after_scheduler':          after_scheduler.state_dict(),
                    },
                    is_best_valid_top1, checkpoint=args.checkpoint)
            elif test_loader is not None:
                logging.save_checkpoint(
                    {
                        'epoch': epoch + 1,
                        'state_dict':               model.state_dict(),
                        'top1':                     top1,
                        'top5':                     top5,
                        'optimizer':                optimizer.state_dict(),
                        'amp':                      scaler.state_dict() if args.amp else None,
                        'args_dict':                args_dict,
                        'scheduler':                scheduler.state_dict(),
                        'after_scheduler':          after_scheduler.state_dict(),
                    },
                    is_best_test_top1, checkpoint=args.checkpoint)
        
            print("Training end time per one epoch: {}s, {}s".format(str(datetime.timedelta(seconds=(time.time()-start_time))), time.time()-start_time))
        # log test/valid loss and accuracy on tensorboard
        if loss['valid']:
            writer.log_scalars('valid', {'loss': loss['valid'], 'top1': top1['valid']}, epoch)
        if loss['test']:
            writer.log_scalars('test', {'loss': loss['test'], 'top1': top1['test']}, epoch)

        # reset DALI iterators
        if args.dali:
            train_loader.reset()
            if valid_loader is not None:
                valid_loader.reset()
            if test_loader is not None:
                test_loader.reset()

    writer.close() # closing the tensorboard summarywriter.
    logger.close()

    if args.rank == 0:
        if valid_loader is not None:
            print(f"Best valid top1: {top1['best_valid']:.2f}")
            print(f"Best valid top5: {top5['best_valid']:.2f}")
            print(f"Test top1 @ best valid top1: {top1['test_at_best_valid_top1']:.2f}")
            print(f"Test top5 @ best valid top1: {top5['test_at_best_valid_top1']:.2f}")
        elif test_loader is not None:
            print(f"Best test top1: {top1['best_test']:.2f}")
            print(f"Best test top5: {top5['best_test']:.2f}")
        print(f"Test top1 @ last epoch: {top1['test']:.4f}")
        print(f"Test top5 @ last epoch: {top5['test']:.4f}")

    # write report
    if args.report:
        if os.path.isfile(args.report):
            df = pd.read_pickle(args.report)
        else:
            df = pd.DataFrame()

        save_dict = vars(args)
        save_dict['Best Valid Top1'] = top1['best_valid']
        save_dict['Best Valid Top5'] = top5['best_valid']
        save_dict['Test Top1 @ best valid top1'] = top1['test_at_best_valid_top1']
        save_dict['Test Top5 @ best valid top1'] = top5['test_at_best_valid_top1']
        save_dict['Test Top1'] = top1['test']
        save_dict['Test Top5'] = top5['test']
        df = df.append(save_dict, ignore_index=True)
        df.to_pickle(args.report)

    if args.distributed:
        dist.destroy_process_group()
    
    if args.rank == 0:
        print("Training end time: ", str(datetime.timedelta(seconds=(time.time()-start_time))))
if __name__ == '__main__':
    main()
