import json
import argparse
from os.path import join
from tqdm import tqdm

from models.aae import AAE

import wandb
import torch
import torch.nn as nn
import torch.nn.functional as F
import pytorch_lightning as pl

def models_sanity_check(code, ground_truth, encoder, pointnet, generator):
    assert not ground_truth.requires_grad, "Ground truth does not require gradients"
    assert code.requires_grad, "Input does require gradients"

    assert (not encoder.training and not pointnet.training and not generator.training), "The models need to be in evaluation mode"

    # Encoder must be freezed
    for param in encoder.parameters():
        assert not param.requires_grad, "The comparator model must be frozen"

    # PointNet must be freezed
    for param in pointnet.parameters():
        assert not param.requires_grad, "The comparator model must be frozen"

    # Generator must be freezed
    for param in generator.parameters():
        assert not param.requires_grad, "The generator model must be frozen"
    
def get_class_distribution(config, logits):
    dataset_name = config['dataset'].lower()
    if dataset_name == 'shapenet':
        from datasets.shapenet import ShapeNetDataset as ShapeNet
        classes = list(ShapeNet.synth_id_to_category.values())
    elif dataset_name == 'modelnet':
        from datasets.modelnet import ModelNet40 as ModelNet
        classes = ModelNet.all_classes
    else:
        raise ValueError(f'Invalid dataset name. Expected `shapenet` or 'f'`modelnet`. Got: `{dataset_name}`')

    probs = F.softmax(logits, dim=1)
    values = probs.squeeze().tolist()
    data = [[label, val] for (label, val) in zip(classes, values)]
    table = wandb.Table(data=data, columns = ["class", "probability"])
    return table

def optimization_loop(config, encoder, pointnet, generator, kl_div, optimizer, code, ground_truth):

    # Optimization loop
    # encoder.eval()
    generator.eval()
    pointnet.eval()
    models_sanity_check(code=code, ground_truth=ground_truth, encoder=encoder, pointnet=pointnet, generator=generator)

    progress_bar = tqdm(range(1), desc='Optimizing', bar_format='{desc} [{elapsed}, {postfix}]')
    it = 0
    convergence = 1
    while convergence > config["threshold"]:
        optimizer.zero_grad()
        
        gen_points = generator(code)
        logits = pointnet(gen_points)

        assert gen_points.max() < 1.5 and gen_points.min() > -1.5
        log_probs = F.log_softmax(logits, dim=1)
        loss = kl_div(log_probs, ground_truth)
        loss.backward()
        optimizer.step()

        convergence = loss.item()
        progress_bar.set_postfix({'loss': convergence, 'iteration': it})
        wandb.log({'loss': convergence,
                   'iteration': it,
                })
        
        it += 1

        if it % 20000 == 0:
            wandb.log({'gen_pointcloud': wandb.Object3D(gen_points.detach().cpu().squeeze().numpy().transpose())})
            
            table = get_class_distribution(config, logits.detach())
            wandb.log({"class_distribution" : wandb.plot.bar(table, "class", "probability", title="Class Distribution")})

    wandb.log({'gen_pointcloud': wandb.Object3D(gen_points.detach().cpu().squeeze().numpy().transpose())})
    
    table = get_class_distribution(config, logits.detach())
    wandb.log({"class_distribution" : wandb.plot.bar(table, "class", "probability", title="Class Distribution")})

def main(config):
    pl.seed_everything(config['seed'])

    # MODELS
    with open(join("settings", config["aae_config"])) as f:
        aae_config = json.load(f)
    aae = AAE.load_from_checkpoint(join(config['ckpts_dir'], config['aae_ckpt']), config=aae_config, map_location=config["device"])
    aae.freeze()
    encoder = aae.encoder.to(config['device'])
    generator = aae.generator.to(config["device"])
    comparator = aae.comparator.to(config["device"])

    # LOSS
    kl_div = nn.KLDivLoss(reduction='batchmean')

    # GROUND TRUTH
    dataset_name = config['dataset'].lower()
    if dataset_name == 'shapenet':
        from datasets.shapenet import ShapeNetDataset as ShapeNet
        cls_idx = ShapeNet.category_to_synth_id[ShapeNet.synth_id_to_number[config['expected_class']]]
        classes_idx = list(ShapeNet.synth_id_to_number.values())
    elif dataset_name == 'modelnet':
        from datasets.modelnet import ModelNet40 as ModelNet
        cls_idx = ModelNet.category_to_number[config['expected_class']]
        classes_idx = list(ModelNet.category_to_number.values())
    else:
        raise ValueError(f'Invalid dataset name. Expected `shapenet` or `modelnet`. Got: `{dataset_name}`')

    gt_class = (torch.Tensor(classes_idx) == cls_idx).unsqueeze(dim=0).float().to(config["device"])

    # INPUT
    encoder.eval()
    dataset = ModelNet(root_dir=join('data', 'modelnet'), split="test")
    for data in dataset:
        if data[1].item() == 2:
            points = data[0].transpose(0,1).unsqueeze(0).to(config['device'])
            gt_idx = data[1]
            break

    # assert cls_idx != gt_idx
    code, _, _ = encoder(points)
    # code = torch.rand( 1, config["z_size"] ).to(config["device"])

    # OPTIMIZER
    optim = getattr(torch.optim, config['optimizer']['type'])
    optim = optim([code.requires_grad_()], 
        **config['optimizer']['hyperparams'])

    wandb.init(id=config["run_id"], project=config["project_name"], config=config, resume="allow")

    optimization_loop(config=config, encoder=encoder, pointnet=comparator, generator=generator, kl_div=kl_div, optimizer=optim, code=code, ground_truth=gt_class)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config', default=None, type=str,
                        help='config file path')
    args = parser.parse_args()

    config = None
    if args.config is not None and args.config.endswith('.json'):
        with open(args.config) as f:
            config = json.load(f)
    assert config is not None

    config["device"] = "cuda" if torch.cuda.is_available() else "cpu"

    main(config)
