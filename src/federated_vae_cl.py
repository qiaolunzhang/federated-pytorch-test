import torch
import torchvision
import torchvision.transforms as transforms

import math
import time

# Variational Clustering: https://arxiv.org/abs/2005.04613
# Also https://arxiv.org/abs/1712.07788

# How many models (==slaves)
K=1
# train K models by Federated learning
# each iteration over a subset of parameters: 1) average 2) pass back average to slaves 3) SGD step
# initialize with pre-trained models (better to use common initialization)
# loop order: loop 0: parameters/layers   {
#               loop 1 : {  averaging (part of the model)
#                loop 2: { epochs/databatches  { train; } } } }
# repeat this Nloop times

# model parameters
Kc=10 # number of clusters
Lc=32 # latent dimension 

torch.manual_seed(69)
# minibatch size
default_batch=128 # no. of batches per model is (50000/K)/default_batch
Nloop=1 # how many loops over the whole network
Nepoch=1 # how many epochs?
Nadmm=1 # how many FA iterations

# regularization
lambda2=0.001 # L2

load_model=False
init_model=True
save_model=True
check_results=True
# if input is biased, each 1/K training data will have
# (slightly) different normalization. Otherwise, same normalization
biased_input=True

# (try to) use a GPU for computation?
use_cuda=True
if use_cuda and torch.cuda.is_available():
  mydevice=torch.device('cuda')
else:
  mydevice=torch.device('cpu')


# split 50000 training data into K subsets (last one will be smaller if K is not a divisor)
K_perslave=math.floor((50000+K-1)/K)
subsets_dict={}
for ck in range(K):
 if K_perslave*(ck+1)-1 <= 50000:
  subsets_dict[ck]=range(K_perslave*ck,K_perslave*(ck+1)-1)
 else:
  subsets_dict[ck]=range(K_perslave*ck,50000)

transforms_dict={}
for ck in range(K):
 if biased_input:
  # slightly different normalization for each subset
  transforms_dict[ck]=transforms.Compose(
   [transforms.ToTensor(),
     transforms.Normalize((0.5+ck/100,0.5-ck/100,0.5),(0.5+ck/100,0.5-ck/100,0.5))])
 else:
  # same normalization for all training data
  transforms_dict[ck]=transforms.Compose(
   [transforms.ToTensor(),
     transforms.Normalize((0.5,0.5,0.5),(0.5,0.5,0.5))])


trainset_dict={}
testset_dict={}
trainloader_dict={}
for ck in range(K):
 trainset_dict[ck]=torchvision.datasets.CIFAR10(root='./torchdata', train=True,
    download=True, transform=transforms_dict[ck])
 trainloader_dict[ck] = torch.utils.data.DataLoader(trainset_dict[ck], batch_size=default_batch, shuffle=False, sampler=torch.utils.data.SubsetRandomSampler(subsets_dict[ck]),num_workers=1)

import numpy as np

# define variational autoencoder
from simple_models import *
from simple_utils  import *

net_dict={}

for ck in range(K):
 net_dict[ck]=AutoEncoderCNNCL(K=Kc,L=Lc).to(mydevice)
 # update from saved models
 if load_model:
   checkpoint=torch.load('./s'+str(ck)+'.model',map_location=mydevice)
   net_dict[ck].load_state_dict(checkpoint['model_state_dict'])
   net_dict[ck].train()

################################################################################ Loss functions
# term 1: E_qk{ log p(x|theta) }
# weighted reconstruction loss
def cost1(pk,px_z_mu,px_z_sig2,x):
 thisbatch_size=x.shape[0]
 err=x-px_z_mu
 err.pow_(2).div_(2*px_z_sig2)
 err1=0.5*torch.log(px_z_sig2*2*math.pi)
 loss=0
 for ci in range(thisbatch_size):
  loss=loss+pk[ci]*torch.sum(err[ci]+err1[ci])
 return loss.div_(thisbatch_size)

# term 2: E_qk{ -log(q(k|x))  }
# sample-wise entropy
def cost2(pk):
 thisbatch_size=pk.shape[0]
 loss=0
 for ci in range(thisbatch_size):
   loss=loss-pk[ci]*torch.log(pk[ci]+1e-9) # add delta to avoid NaN
 return loss.div_(thisbatch_size)

# term 21: E_qk{ log(\barq(k|x))  }
# 1 / batch-wise entropy
def cost21(pk):
 # average over batch size
 pbar=torch.mean(pk,0)
 loss=-pbar*torch.log(pbar+1e-9) # add delta to avoid NaN
 return 1/(loss+1e-9)


# term 3: E_qk{ KL(q(z|x,k)||p(z|k))  }
# KL divergence
def cost3(pk,q_z_mu,q_z_sig2,p_z_mu,p_z_sig2):
 thisbatch_size=pk.shape[0]
 mudiff=p_z_mu-q_z_mu
 mudiff.pow_(2).div_(p_z_sig2)
 sigratio=q_z_sig2/p_z_sig2
 loss=0
 for ci in range(thisbatch_size):
  loss=loss+0.5*pk[ci]*torch.sum(sigratio[ci]-torch.log(sigratio[ci])+mudiff[ci]-1)

 return loss.div_(thisbatch_size)

def loss_function(ekhat,mu_xi,sig2_xi,mu_b,sig2_b,mu_th,sig2_th,x):
  """
    ekhat: q(k|x) Kx1
    each dict item of
    mu_xi[],sig2_xi[] : parametrize q(z|x,k) Lx1
    mu_b[],sig2_b[] : parametrize p(z|k) Lx1
    mu_th[],sig2_th[] : parametrize p(x|z) : size equal to x
    x : data
  """
  # scale up entropy
  alpha=10.0
  beta=1.0
  loss=0
  for ci in range(Kc):
    c1=cost1(ekhat[:,ci],mu_th[ci],sig2_th[ci],x)
    c2=cost2(ekhat[:,ci])
    c21=cost21(ekhat[:,ci])
    c3=cost3(ekhat[:,ci],mu_xi[ci],sig2_xi[ci],mu_b[ci],sig2_b[ci])
    #print("cluster %d costs %f,%f,%f,%f"%(ci,c1.data.item(),c2.data.item(),c21.data.item(),c3.data.item()))
    loss+=c1+alpha*(c2+c3)+beta*c21
  return loss

##############################################################################################

if init_model:
  for ck in range(K):
   # note: use same seed for random number generation
   torch.manual_seed(0)
   net_dict[ck].apply(init_weights)

# get layer ids in given order 0..L-1 for selective training
np.random.seed(0)# get same list
Li=net_dict[0].train_order_block_ids()
L=len(Li)


import torch.optim as optim
from lbfgsnew import LBFGSNew # custom optimizer
############### loop 00 (over the full net)
for nloop in range(Nloop):
  ############ loop 0 (over layers of the network)
  for ci in range(L):
   for ck in range(K):
     unfreeze_one_block(net_dict[ck],ci)
     if ci==2: # latent space
      net_dict[ck].enable_repr()
     else:
      net_dict[ck].disable_repr()
   trainable=filter(lambda p: p.requires_grad, net_dict[0].parameters())
   params_vec1=torch.cat([x.view(-1) for x in list(trainable)])
  
   # number of parameters trained
   N=params_vec1.numel()
   del trainable,params_vec1

   z=torch.empty(N,dtype=torch.float,requires_grad=False).to(mydevice)
   z.fill_(0.0)
  
   opt_dict={}
   for ck in range(K):
     if ci==2:
       opt_dict[ck]=optim.Adam(filter(lambda p: p.requires_grad, net_dict[ck].parameters()),lr=0.0001)
     else:
       opt_dict[ck]=LBFGSNew(filter(lambda p: p.requires_grad, net_dict[ck].parameters()), history_size=10, max_iter=4, line_search_fn=True,batch_mode=True)

  
   ############# loop 1 (Federated avaraging for subset of model)
   for nadmm in range(Nadmm):
     ##### loop 2 (data) (all network updates are done per epoch, because K is large
     ##### and data per host is assumed to be small)
     for epoch in range(Nepoch):

        #### loop 3 (models)
        for ck in range(K):
          running_loss=0.0
  
          for i,(images, _) in enumerate(trainloader_dict[ck],0): # ignore labels
            # get the inputs
            x=Variable(images).to(mydevice)


            def closure1():
               ekhat,mu_xi,sig2_xi,mu_b,sig2_b,mu_th,sig2_th=net_dict[ck](x)
               if torch.is_grad_enabled():
                 opt_dict[ck].zero_grad()
               loss=loss_function(ekhat,mu_xi,sig2_xi,mu_b,sig2_b,mu_th,sig2_th,x)
               trainable=filter(lambda p: p.requires_grad, net_dict[ck].parameters())
               params_vec1=torch.cat([x.view(-1) for x in list(trainable)])
               loss+=lambda2*torch.norm(params_vec1,2)**2
               if loss.requires_grad:
                 loss.backward()
               return loss
  
            # ADMM step 1
            opt_dict[ck].step(closure1)
  
            # only for diagnostics
            ekhat,mu_xi,sig2_xi,mu_b,sig2_b,mu_th,sig2_th=net_dict[ck](x)
            loss1=loss_function(ekhat,mu_xi,sig2_xi,mu_b,sig2_b,mu_th,sig2_th,x)
            running_loss +=float(loss1)

            for k in range(Kc):
              c1=cost1(ekhat[:,k],mu_th[k],sig2_th[k],x)
              c2=cost2(ekhat[:,k])
              c21=cost21(ekhat[:,k])
              c3=cost3(ekhat[:,k],mu_xi[k],sig2_xi[k],mu_b[k],sig2_b[k])
              print("cluster %d costs %f,%f,%f,%f"%(k,c1.data.item(),c2.data.item(),c21.data.item(),c3.data.item()))

            print('model=%d block=[%d,%d] %d(%d) minibatch=%d epoch=%d loss %e'%(ck,Li[ci][0],Li[ci][1],nloop,N,i,epoch,loss1))
            del x,loss1,ekhat,mu_xi,sig2_xi,mu_b,sig2_b,mu_th,sig2_th
         

        # Federated averaging
        x_dict={}
        for ck in range(K):
          x_dict[ck]=get_trainable_values(net_dict[ck],mydevice)

        znew=torch.zeros(x_dict[0].shape).to(mydevice)
        for ck in range(K):
         znew=znew+x_dict[ck]
        znew=znew/K

        dual_residual=torch.norm(z-znew).item()/N # per parameter
        print('dual (epoch=%d,loop=%d,block=[%d,%d],avg=%d)=%e'%(epoch,nloop,Li[ci][0],Li[ci][1],nadmm,dual_residual))
        z=znew
        for ck in range(K):
          put_trainable_values(net_dict[ck],z)


print('Finished Training')


if save_model:
 for ck in range(K):
   torch.save({
     'model_state_dict':net_dict[ck].state_dict(),
     'epoch':epoch,
     'optimizer_state_dict':opt_dict[ck].state_dict(),
     'running_loss':running_loss,
     },'./s'+str(ck)+'.model')
