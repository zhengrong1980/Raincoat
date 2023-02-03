# -*- coding: utf-8 -*-
import numpy as np
import sys
sys.path.append('D:\\Transfer-Learning-Time-Series\\Transfer-Learning-Time-Series')
import torch

import torch.nn as nn
import torch.nn.functional as F
import matplotlib.pyplot as plt
from dataloader.dataloader import data_generator
from configs.data_model_configs import get_dataset_class
from configs.hparams import get_hparams_class
from sklearn.metrics import f1_score, accuracy_score
from models import classifier

import seaborn as sn


class Algorithm(torch.nn.Module):
    """
    A subclass of Algorithm implements a domain adaptation algorithm.
    Subclasses should implement the update() method.
    """

    def __init__(self, configs):
        super(Algorithm, self).__init__()
        self.configs = configs
        self.cross_entropy = nn.CrossEntropyLoss()

    def update(self, *args, **kwargs):
        raise NotImplementedError

def evaluate(feature_extractor, classifier, eval_loader):
    feature_extractor.eval()
    classifier.eval()
    total_loss_ = []
    trg_pred_labels = np.array([])
    trg_true_labels = np.array([])
   
    all_features = []
    with torch.no_grad():
        for data, labels in eval_loader:
            data = data.float().to('cuda')
            labels = labels.view((-1)).long().to('cuda')
            m1,m2,m3, features = feature_extractor(data)
            predictions = classifier(features)
            loss = F.cross_entropy(predictions, labels)
            total_loss_.append(loss.item())
            pred = predictions.detach().argmax(dim=1)  # get the index of the max log-probability

            trg_pred_labels = np.append(trg_pred_labels, pred.cpu().numpy())
            trg_true_labels = np.append(trg_true_labels, labels.data.cpu().numpy())
            all_features.append(features.cpu().numpy())
    trg_loss = torch.tensor(total_loss_).mean()  # average loss
    f1 = f1_score(trg_pred_labels, trg_true_labels, pos_label=None, average="weighted")
    all_features = np.vstack(all_features)
    labels = np.vstack(trg_true_labels)
    return trg_loss, accuracy_score(trg_true_labels, trg_pred_labels), f1, all_features, labels


class SpectralConv1d(nn.Module):
    def __init__(self, in_channels, out_channels, modes1):
        super(SpectralConv1d, self).__init__()

        """
        1D Fourier layer. It does FFT, linear transform, and Inverse FFT.    
        """

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.modes1 = modes1  #Number of Fourier modes to multiply, at most floor(N/2) + 1

        self.scale = (1 / (in_channels*out_channels))
        self.weights1 = nn.Parameter(self.scale * torch.rand(in_channels, out_channels, self.modes1, dtype=torch.cfloat))

    # Complex multiplication
    def compl_mul1d(self, input, weights):
        # (batch, in_channel, x ), (in_channel, out_channel, x) -> (batch, out_channel, x)
        return torch.einsum("bix,iox->box", input, weights)

    def forward(self, x):
        batchsize = x.shape[0]
        #Compute Fourier coeffcients up to factor of e^(- something constant)
        x_ft = torch.fft.rfft(x).cfloat()

        # Multiply relevant Fourier modes
        perm = torch.randperm(self.modes1)
        idx = perm[:self.modes1]
        out_ft = torch.zeros(batchsize, self.out_channels, x.size(-1)//2 + 1,  device=x.device, dtype=torch.cfloat)
        noise = torch.rand(batchsize, self.out_channels,self.out_channels)
        eye =  torch.eye(self.out_channels, x.size(-1)//2 + 1)
        out_ft[:, :, idx] = self.compl_mul1d(x_ft[:, :, idx], self.weights1) 
        x = torch.fft.irfft(out_ft, n=x.size(-1))
        mag =  torch.sqrt(out_ft.real**2+out_ft.imag**2)
        # phase = torch.angle(out_ft)
        # ft = torch.concat([phase, mag],dim=-1)
        return mag, x

class fno(nn.Module):
    def __init__(self, configs):
        super(fno, self).__init__()
        self.modes1 = 32
        self.width = configs.input_channels
        self.channel = configs.input_channels
        self.fl =   configs.sequence_len
        self.fc0 = nn.Linear(self.channel, self.width) # input channel is 2: (a(x), x)

        self.conv0 = SpectralConv1d(self.width, self.width, self.modes1)
        self.conv1 = SpectralConv1d(self.width, self.width, self.modes1)
        self.conv2 = SpectralConv1d(self.width, self.width, self.modes1)
        self.w0 = nn.Conv1d(self.width, self.width, 1)
        self.w1 = nn.Conv1d(self.width, self.width, 1)
        self.w2 = nn.Conv1d(self.width, self.width, 1)
        self.nn = nn.LayerNorm([3, 128])
        self.nn2 = nn.LayerNorm([3, 128])
        self.conv_block1 = nn.Sequential(
            nn.Conv1d(configs.input_channels, configs.mid_channels, kernel_size=configs.kernel_size,
                      stride=configs.stride, bias=False, padding=(configs.kernel_size // 2)),
            nn.BatchNorm1d(configs.mid_channels),
            nn.ReLU(),
            nn.MaxPool1d(kernel_size=2, stride=2, padding=1),
            nn.Dropout(configs.dropout)
        )

        self.conv_block2 = nn.Sequential(
            nn.Conv1d(configs.mid_channels, configs.mid_channels * 2, kernel_size=8, stride=1, bias=False, padding=4),
            nn.BatchNorm1d(configs.mid_channels * 2),
            nn.ReLU(),
            nn.MaxPool1d(kernel_size=2, stride=2, padding=1)
        )

        self.conv_block3 = nn.Sequential(
            nn.Conv1d(configs.mid_channels * 2, configs.final_out_channels, kernel_size=8, stride=1, bias=False,
                      padding=4),
            nn.BatchNorm1d(configs.final_out_channels),
            nn.ReLU(),
            nn.MaxPool1d(kernel_size=2, stride=2, padding=1),
        )
        self.adaptive_pool = nn.AdaptiveAvgPool1d(configs.features_len)
    def forward(self, x):
        mag, x1 = self.conv0(x)
        x2 = self.w0(x)
        x = x1 + x2
        x = self.nn(x)
        x = F.gelu(x)
        mag2, x1 = self.conv1(x)
        x2 = self.w1(x)
        x = x1 + x2
        x = self.nn2(x)
        x = F.gelu(x)
        mag3, x1 = self.conv2(x)
        x2 = self.w2(x)
        x = x1 + x2
        x = self.conv_block1(x)
        x = self.conv_block2(x)
        x = self.conv_block3(x)
        x = self.adaptive_pool(x)
        x_flat = x.reshape(x.shape[0], -1)
        return mag,mag2,mag3, x_flat

def get_configs():
    dataset_class = get_dataset_class('WISDM')
    hparams_class = get_hparams_class('WISDM')
    return dataset_class(), hparams_class()

class Huan(Algorithm):
    def __init__(self, backbone_fe, configs, hparams, device):
        super(Huan, self).__init__(configs)
        self.feature_extractor = backbone_fe(configs)
        self.classifier = classifier(configs)
        self.network = nn.Sequential(self.feature_extractor, self.classifier).to('cuda')

        self.mse = nn.cross_entropy
        self.optimizer = torch.optim.Adam(
            list(self.simclr.parameters()) + list(self.classifier.parameters()),
            lr=1*hparams["learning_rate"],
            weight_decay=hparams["weight_decay"], betas=(0.5, 0.99)
        )
        self.hparams = hparams
        self.device = device
        
    # def update(self, src_x, src_y, trg_x):


    #     # zero grad
    #     self.optimizer.zero_grad()
    #     m1, m2, m3, src_feat = self.feature_extractor(src_x)
    #     # trg_feat, zt = self.simclr(trg_x)
    #     src_pred = self.classifier(src_feat)
    #     # trg_feat = self.feature_extractor(trg_x)
    #     # Task classification  Loss
    #     src_cls_loss = self.cross_entropy(src_pred.squeeze(), src_y)
    #     mag,mag2,mag3, rt = self.fno(trg_x)    
    #     recons = self.mse(trg_x, rt)
    #     recons.backward(retain_graph=True)
        
    #     loss =  src_cls_loss + 3* domain_loss 
    #     loss.backward()
    #     self.optimizer.step()
    #     self.optimizer2.step()
    #     # self.ema.update()
    #     return {'Total_loss': loss.item(), 'Domain_loss': domain_loss.item(), \
    #             'Src_cls_loss': src_cls_loss.item(),'Recon_loss': recons.item()}
    
    
data_path = 'D:\\Transfer-Learning-Time-Series\\Transfer-Learning-Time-Series\\data\\WISDM'
hparams = {"batch_size":32}


dataset_configs, hparams_class = get_configs()
dataset_configs.final_out_channels = dataset_configs.final_out_channels

src_train_dl, src_test_dl = data_generator(data_path, '7' ,dataset_configs,hparams)
trg_train_dl, trg_test_dl = data_generator(data_path, '18', dataset_configs,hparams)
device = 'cuda'
model = fno(dataset_configs).to(device)
classifier = classifier(dataset_configs).to(device)
optimizer = torch.optim.Adam(
    list(model.parameters()) + list(classifier.parameters()),
    lr=1e-3,
    weight_decay=1e-4, betas=(0.5, 0.99))
CE = nn.CrossEntropyLoss()
src_loaders = enumerate(zip(src_train_dl))
for e in range(10):
    model.train()
    for src_x, src_y in src_train_dl:
        optimizer.zero_grad()
        src_x, src_y = src_x.float().to(device), src_y.long().to(device)
        m1,m2,m3, src_feat = model(src_x)
        src_pred = classifier(src_feat)
        loss = CE(src_pred.squeeze(), src_y)
        loss.backward()
        optimizer.step()
    lt, acc,f1,all_features, labels = evaluate(model, classifier, src_test_dl)
    print(acc.item(),f1)


lt, acc,f1,all_features2, labels2 = evaluate(model, classifier, trg_test_dl)

from sklearn.manifold import TSNE
import matplotlib.pyplot as plt
from matplotlib import cm
import numpy as np

src_f, src_y, trg_f, trg_y = all_features, labels, all_features2,labels2


all_f = np.vstack((src_f, trg_f))

tsne = TSNE(2, verbose=1)
tsne_proj = tsne.fit_transform(all_f)

# Plot those points as a scatter plot and label them based on the pred labels
cmap = cm.get_cmap('tab20')
cmap2 = cm.get_cmap('GnBu')
fig, ax = plt.subplots(figsize=(8,8))
num_categories = 6
colors = ['red','red','red','red','red','red']
for activity in range(num_categories):
    indices = np.squeeze(trg_y==activity)
    indices2 = np.squeeze(src_y==activity)
    tsne_src, tsne_trg = tsne_proj[0:len(src_f)], tsne_proj[len(src_f):]
    ax.scatter(tsne_src[indices2,0],tsne_src[indices2,1], s=50,\
                marker='o',c=np.array(cmap(activity)).reshape(1,4), label = activity)
    ax.scatter(tsne_trg[indices,0],tsne_trg[indices,1],s=50,  \
                marker='s',c=np.array(cmap(activity)).reshape(1,4), label = activity)
    # sn.kdeplot(tsne_src[indices2,0],tsne_src[indices2,1],shade=True,\
    #             labels=str(activity),c=np.array(cmap(activity)),alpha=0.2)
    # sn.kdeplot(tsne_trg[indices,0],tsne_trg[indices,1],shade=True,\
    #             labels=activity,c=np.array(cmap2(activity)),alpha=0.5)
    
ax.legend(fontsize='large', markerscale=2)
plt.show()

