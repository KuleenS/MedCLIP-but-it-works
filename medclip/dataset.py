import os
from collections import defaultdict
import pdb

from PIL import Image
import torch
from torch.utils.data import Dataset, DataLoader
from torchvision.io import read_image
from transformers import CLIPProcessor, CLIPModel, CLIPFeatureExtractor, CLIPTokenizer

import pandas as pd
import numpy as np
from .feature_extractor import MedCLIPFeatureExtractor

class IUXRayDataset(Dataset):
    '''
    # how to crop raw images into patches
    res=rearrange(x_frontal[:,None,:9*224,:11*224], 'b c (h p1) (w p2) -> b (h w c) p1 p2', p1=224,p2=224)
    '''
    _report_sections_ = ['findings','impression','MeSH']
    channel_num = 1 # XRay is a gray scale image
    img_mean = [0.5862785803043838]
    img_std = [0.27950088968644304]
    def __init__(self, datadir):
        self.image_dir = os.path.join(datadir, './images/images_normalized')
        reports = pd.read_csv(os.path.join(datadir, 'indiana_reports.csv'), index_col=0)
        projection = pd.read_csv(os.path.join(datadir, 'indiana_projections.csv'), index_col=0)
        # drop NaN findings and impressions
        not_null_idx = ~(reports['findings'].isnull() * reports['impression'].isnull())
        reports = reports[not_null_idx][self._report_sections_]
        df_frontal = projection[projection['projection']=='Frontal']
        df_lateral = projection[projection['projection']=='Lateral']

        self.uid2frontal = defaultdict(list)
        self.uid2lateral = defaultdict(list)

        for idx in reports.index.tolist():
            if idx in df_frontal.index:
                names = df_frontal.loc[idx].filename
                if isinstance(names, str): self.uid2frontal[idx].append(os.path.join(self.image_dir, names))
                else: self.uid2frontal[idx].extend([os.path.join(self.image_dir,name) for name in names.tolist()])
            if idx in df_lateral.index:
                names = df_lateral.loc[idx].filename
                if isinstance(names, str): self.uid2lateral[idx].append(os.path.join(self.image_dir, names))
                else: self.uid2lateral[idx].extend([os.path.join(self.image_dir,name) for name in names.tolist()])

        self.reports = reports.reset_index()
        # check if one report does have both frontal and lateral image
        f_uid_list = list(self.uid2frontal.keys())
        l_uid_list = list(self.uid2lateral.keys())
        x1 = [x for x in reports.index.tolist() if x not in f_uid_list]
        x2 = [x for x in reports.index.tolist() if x not in l_uid_list]
        print(np.intersect1d(x1, x2))

    def compute_img_mean_std(self):
        pixel_num  = 0
        channel_sum = np.zeros(self.channel_num)
        channel_sum_squared = np.zeros(self.channel_num)
        for index in self.reports.index.tolist():
            uid = self.reports.iloc[index].uid
            print('compute image mean and std, uid: ', uid)
            for filename in self.uid2frontal[uid]:
                x_image = read_image(filename) # 1, 2048, 2496
                img = x_image / 255
                pixel_num += torch.prod(torch.tensor(img.shape)).item()
                channel_sum += torch.sum(img).item()
                channel_sum_squared += torch.sum(img.square()).item()

            for filename in self.uid2lateral[uid]:
                x_image = read_image(filename)
                img = x_image / 255
                pixel_num += torch.prod(torch.tensor(img.shape)).item()
                channel_sum += torch.sum(img).item()
                channel_sum_squared += torch.sum(img.square()).item()

        img_mean = channel_sum / pixel_num
        img_std = np.sqrt(channel_sum_squared/pixel_num - np.square(img_mean))
        return {'mean':img_mean[0], 'std':img_std[0]}

    def __len__(self):
        return len(self.reports)

    def __getitem__(self, idx):
        '''return 
        1. list of frontal images
        2. list of lateral images
        3. the report texts
        4. the normal/abnormal label
        '''
        report = self.reports.iloc[idx]
        uid = report.uid
        report_str = ' '.join(report[self._report_sections_].fillna(' ').values.tolist())
        f_list = []
        for filename in self.uid2frontal[uid]:
            x_image = Image.open(filename)
            f_list.append(x_image)
        l_list = []
        for filename in self.uid2lateral[uid]:
            x_image = Image.open(filename)
            l_list.append(x_image)
        return {'frontal': f_list, 'lateral': l_list, 'report': report_str, 'label': report['MeSH']}

# ########
# Three collators for three contrastive loss computation
# ########
class IUXRayCollatorBase:
    def __init__(self,
        feature_extractor=None,
        tokenizer=None,
        img_mean=None,
        img_std=None,
        max_text_length=77,
        ):
        if feature_extractor is None:
            assert img_mean is not None
            assert img_std is not None
            self.feature_extractor = MedCLIPFeatureExtractor(
                do_resize=True,
                size=224,
                resample=3,
                do_center_crop=True,
                crop_size=224,
                do_normalize=True,
                image_mean=img_mean,
                image_std=img_std,
            )
        else:
            self.feature_extractor = feature_extractor
        if tokenizer is None:
            self.tokenizer = CLIPTokenizer.from_pretrained('openai/clip-vit-base-patch32')
        else:
            self.tokenizer = tokenizer
        
        self.tokenizer.model_max_length = max_text_length
    
    def __call__(self, x):
        raise NotImplementedError

class IUXRayImageTextCollator(IUXRayCollatorBase):
    def __init__(self, feature_extractor=None, tokenizer=None, img_mean=None, img_std=None, is_train=False):
        '''return image-text report positive pairs
        '''
        super().__init__(feature_extractor, tokenizer, img_mean, img_std)
        self.is_train = is_train

    def __call__(self, x):
        # x: list of dict{frontal, lateral, report}
        # return {'input_ids': [], 'pixel_values': []}
        inputs = defaultdict(list)
        text_list = []
        for data in x: # every data is a single patient
            report = data['report']
            if self.is_train: report = self._text_random_cut_(report)
            if len(data['frontal']) > 0:
                images = self.feature_extractor(data['frontal'], return_tensors='pt')
                inputs['pixel_values'].append(images['pixel_values'])
                text_list.extend([report] * len(data['frontal']))
            if len(data['lateral']) > 0:
                images = self.feature_extractor(data['lateral'], return_tensors='pt')
                inputs['pixel_values'].append(images['pixel_values'])
                text_list.extend([report] * len(data['lateral']))
        # tokenize texts together
        text_token_ids = self.tokenizer(text_list, return_tensors='pt', padding=True, truncation=True)
        for key in text_token_ids.keys():
            inputs[key] = text_token_ids[key]
        inputs['pixel_values'] = torch.cat(inputs['pixel_values'])
        return inputs

    def _text_random_cut_(self, text):
        token_list = text.split(' ')
        max_start_idx = np.maximum(len(token_list)-self.tokenizer.model_max_length, 0)
        if max_start_idx == 0:
            return text
        else:
            start_idx = np.random.randint(0, max_start_idx)
            return ' '.join(token_list[start_idx:start_idx+self.tokenizer.model_max_length])

class IUXRayAbnormalNormalCollator(IUXRayCollatorBase):
    def __init__(self, feature_extractor=None, tokenizer=None, img_mean=None, img_std=None, is_train=False):
        '''return abnormal-normal positive pairs,
        normal: label 0
        abnormal: label 1
        '''
        super().__init__(feature_extractor, tokenizer, img_mean, img_std)
        self.is_train = is_train

    def __call__(self, x):
        pdb.set_trace()
        pass


class IUXRayFrontalLateralCollator:
    def __init__(self):
        '''return frontal-lateral positive pairs
        '''
        pass