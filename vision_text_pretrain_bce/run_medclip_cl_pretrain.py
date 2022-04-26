import pdb, os
import random

import numpy as np
import torch
from torch.utils.data import DataLoader

from medclip.modeling_medclip import MedClipModel, MedClipPromptClassifier
from medclip.dataset import ImageTextContrastiveDataset, ZeroShotImageDataset
from medclip.dataset import ImageTextContrastiveCollator, ZeroShotImageCollator
from medclip.losses import ImageTextContrastiveLoss
from medclip.trainer import Trainer
from medclip.evaluator import Evaluator


#TODO experiments
# 1. try use another evaluation task since zero-shot classification might not be the best
# use 14-class linear probe as the evaluation; design a linear probe dataset and the classifier
# 2. try to do zero-shot covid19 classification: binary classification, covid or not using covid description texts
# 3. try to do data-efficient experiments with different amount of samples
# 4. try to do update prompt sentences after each epoch!!!

# set random seed
seed = 42
random.seed(seed)
np.random.seed(seed)
torch.manual_seed(seed)
torch.cuda.manual_seed(seed)
os.environ['PYTHONASHSEED'] = str(seed)
os.environ['TOKENIZERS_PARALLELISM']='false'

# set cuda devices
os.environ['CUDA_VISIBLE_DEVICES']='1'
device = "cuda:0" if torch.cuda.is_available() else "cpu"

# set training configurations
train_config = {
    'batch_size': 100,
    'num_epochs': 10,
    'warmup': 0.1, # the first 10% of training steps are used for warm-up
    'lr': 5e-5,
    'weight_decay': 1e-4,
    'eval_batch_size': 128,
    'eval_steps': 1000,
    'save_steps': 1000,
}

# only pretrain on chexpert train data and mimic-cxr data
# do zero-shot training on chexpert-5x200 and iuxray
datalist = [
    'chexpert-train',
    'mimic-cxr',
]

# datalist = [
#     'mimic-cxr',
# ]

traindata = ImageTextContrastiveDataset(datalist=datalist)
train_collate_fn = ImageTextContrastiveCollator()
trainloader = DataLoader(traindata, 
    batch_size=train_config['batch_size'], 
    collate_fn=train_collate_fn, 
    shuffle=True,
    pin_memory=True,
    num_workers=12,
    )

# build medclip model
model = MedClipModel()
model.cuda()

# build evaluator
val_data = ZeroShotImageDataset(['chexpert-5x200'])
val_collate_fn = ZeroShotImageCollator()
eval_dataloader = DataLoader(val_data,
    batch_size=train_config['eval_batch_size'],
    collate_fn=val_collate_fn,
    shuffle=False,
    pin_memory=True,
    num_workers=0,
    )
medclip_clf = MedClipPromptClassifier(model)
evaluator = Evaluator(
    medclip_clf=medclip_clf,
    eval_dataloader=eval_dataloader,
)

# build loss models and start training
loss_model = ImageTextContrastiveLoss(model)
loss_model.cuda()
train_objectives = [
    (trainloader, loss_model, 1),
]
model_save_path = f'./checkpoints/vision_text_pretrain'
trainer = Trainer()
trainer.train(
    model,
    train_objectives=train_objectives,
    warmup_ratio=train_config['warmup'],
    epochs=train_config['num_epochs'],
    optimizer_params={'lr':train_config['lr']},
    output_path=model_save_path,
    evaluation_steps=train_config['eval_steps'],
    weight_decay=train_config['weight_decay'],
    save_steps=train_config['save_steps'],
    evaluator=evaluator,
    eval_dataloader=eval_dataloader,
    use_amp=True,
    )
print('done')








