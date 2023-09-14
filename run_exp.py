from load_model import *
from helper import set_seed, generate_diag_matrix
from copy import deepcopy
from tqdm import tqdm
from torch import optim
import wandb, argparse

class approx_fnn:
    def __init__(
        self,
        width,
        target_depth,
        frozen_depth,
        rank,
        use_bias,
        activation,
        std = .25,
        method = 'ours',
        batch_size = 5000,
        n_epochs = 1000,
        lr = 1e-3,
        n_test = 10000,
        weight_decay = 0,
        log_wandb = 0,
        init_mode = 'default',
    ):
        set_seed()
        
        self.width = width
        self.target_depth = target_depth
        self.frozen_depth = frozen_depth
        self.rank = rank
        self.use_bias = use_bias
        self.wandb = log_wandb
        
        self.init_models(std, activation, init_mode)
        
        self.criterion = nn.MSELoss()
        
        # perform finetune
        if method == 'ours':
            adapted_m = self.adapt_fnn_ours(batch_size = batch_size)
        elif method == 'sgd':
            adapted_m = self.adapt_fnn_sgd(
                n_epochs = n_epochs,
                batch_size = batch_size,
                lr = lr,
                weight_decay = weight_decay,
            )
        else:
            raise NotImplementedError(f"We only support ours and sgd for parameter method, and {method} is not supported.")
        
        # evaluate the adapted model
        self.eval(adapted_m, n_test = n_test)
        
    def init_models(
        self,
        std,
        activation,
        init_mode,
    ):
        # randomly initialize the target model
        self.target_m = FNN(
            depth = self.target_depth,
            width = self.width,
            rank = self.rank,
            std = std,
            use_bias = self.use_bias,
            apply_lora = False,
            activation = activation,
        )
        
        # randomly initialize the frozen model
        self.frozen_m = FNN(
            depth = self.frozen_depth,
            width = self.width,
            rank = self.rank,
            std = std,
            use_bias = self.use_bias,
            apply_lora = True,
            activation = activation,
        )
        
        if init_mode == 'uniform_singular_values':
            tdl = self.frozen_depth // self.target_depth
            for i in range(self.target_depth):
                # use range(l1, l2) layers in the adapted model to approximate the ith layer in the target model 
                l1 = i * tdl
                l2 = (i + 1) * tdl if i < self.target_depth - 1 else self.frozen_depth
                
                # compute the product of the frozen matrices
                frozen_prod_weight = torch.eye(self.width)
                for l in range(l1, l2):
                    frozen_prod_weight = self.frozen_m.linearlist[l].weight.data @ frozen_prod_weight 
                    
                # set the target weight
                discrepency_matrix = self.target_m.linearlist[i].weight.data - frozen_prod_weight
                self.target_m.linearlist[i].weight.data = frozen_prod_weight + torch.eye(self.width) * torch.mean(torch.svd(discrepency_matrix)[1])

    def adapt_fnn_sgd(
        self,
        n_epochs,
        batch_size,
        lr,
        weight_decay = 0,
    ):
        set_seed()
        
        adapted_m = deepcopy(self.frozen_m)
        
        # specify the lora adapter as the parameters to be optimized
        params = []
        for l in range(self.frozen_depth):
            params.append({'params': adapted_m.loralist[l].lora_A, 'lr': lr, 'weight_decay': weight_decay})
            params.append({'params': adapted_m.loralist[l].lora_B, 'lr': lr, 'weight_decay': weight_decay})
            
        opt = optim.Adam(params)
            
        # Initialize tqdm
        iter_obj = tqdm(range(n_epochs))
        # finetuning
        for i in iter_obj:
            # generate random input from some Gaussian distribution
            x_train = torch.randn(batch_size, self.width) 
            y_train = self.target_m(x_train).detach()
            y_train.requires_grad = False
            
            y_pred = adapted_m(x_train)
            self.train_loss = self.criterion(y_pred, y_train)
            
            if self.wandb:
                wandb.log({'train_loss': self.train_loss.item()})
            
            opt.zero_grad()
            self.train_loss.backward()
            opt.step()
            
            # update tqdm description with current loss
            iter_obj.set_description(f"Loss of SGD: {self.train_loss.item():.4f}")      
            
        # validation
        adapted_m.eval()
        x_val =  torch.randn(batch_size, self.width) 
        y_val = self.target_m(x_val).detach()
        y_val.requires_grad = False
        
        y_pred = adapted_m(x_val)
        self.val_loss = self.criterion(y_pred, y_val)
        
        if self.wandb:
            wandb.log({'val_loss': self.val_loss.item()})
        else:
            print(f"Validation loss: {self.val_loss.item():.4f}")
        
        return adapted_m
    
    def adapt_fnn_ours(
        self,
        batch_size,
    ):
        set_seed()
        
        tdl = self.frozen_depth // self.target_depth
        adapted_m = deepcopy(self.frozen_m)
        
        if self.use_bias:
            # generate random input from some Gaussian distribution
            z = torch.randn(batch_size, self.width) 
        
        lora_adapter = {}
        for i in range(self.target_depth):
            # use range(l1, l2) layers in the adapted model to approximate the ith layer in the target model 
            l1 = i * tdl
            l2 = (i + 1) * tdl if i < self.target_depth - 1 else self.frozen_depth
            
            # compute the product of the frozen matrices
            frozen_prod_weight = torch.eye(self.width)
            frozen_prod_weight_l2L = {(l2-1): frozen_prod_weight}
            for l in range(l1+1, l2)[::-1]:
                frozen_prod_weight = frozen_prod_weight @ adapted_m.linearlist[l].weight.data
                frozen_prod_weight_l2L[l-1] = frozen_prod_weight
            frozen_prod_weight = frozen_prod_weight @ adapted_m.linearlist[l1].weight.data
            
            # get the target weight
            target_weight = self.target_m.linearlist[i].weight.data
            
            # compute the discrepancy matrix
            discrepancy_weight = target_weight - frozen_prod_weight
            
            # perform SVD on the discrepancy matrix
            _, S, V = torch.svd(discrepancy_weight)
            
            if self.wandb:
                wandb.log({'singular_values': S})
            else:
                print(f"Singular values: {S}")
            
            # compute the lora adapter for each layer
            adapted_prod_weight = torch.eye(self.width)
            for l in range(l1, l2):
                # compute the lora adapter for the lth layer
                Ql = V @ generate_diag_matrix(self.width, min(self.rank*(l-l1), self.width), min(self.rank*(l-l1+1), self.width)) @ V.T
                lora_adapter[l] = torch.inverse(frozen_prod_weight_l2L[l]) @ discrepancy_weight @ Ql @ torch.inverse(adapted_prod_weight)
                adapted_prod_weight = (adapted_m.linearlist[l].weight.data + lora_adapter[l]) @ adapted_prod_weight
                
                # update the lora weights in the adapter model
                U_Q, S_Q, V_Q = torch.svd(lora_adapter[l])
                adapted_m.loralist[l].lora_A.data = U_Q @ torch.diag(S_Q)
                adapted_m.loralist[l].lora_B.data = V_Q
            
            # update the bias in the adapter model
            if self.use_bias:
                calibrate_bias = torch.zeros(self.width)
                adapted_prod_weight_rev = torch.eye(self.width)
                adapted_prod_weight_l2L = {(l2-1): adapted_prod_weight_rev}
                for l in range(l1+1, l2)[::-1]:
                    adapted_prod_weight_rev = adapted_prod_weight_rev @ (adapted_m.linearlist[l].weight.data + adapted_m.loralist[l].lora_A @ adapted_m.loralist[l].lora_B.T)
                    adapted_prod_weight_l2L[l-1] = adapted_prod_weight_rev
                
                for l in range(l1, l2):
                    
                    # update the intermediate output without the bias
                    z = z @ adapted_m.linearlist[l].weight.data.T + adapted_m.loralist[l].forward(z)
                    
                    # update the bias in the adapter model
                    if l < l2-1:
                        # ensure all the relus are activated
                        adapted_m.linearlist[l].bias.data = - 2 * min(torch.min(z).item(), 0) * torch.ones(self.width)
                        
                        # update the intermediate output
                        z = z + adapted_m.linearlist[l].bias.data
                        
                        # update the calibrated bias
                        calibrate_bias = calibrate_bias + adapted_prod_weight_l2L[l] @ adapted_m.linearlist[l].bias.data
                    else:
                        # matching the bias in the target model
                        adapted_m.linearlist[l].bias.data = self.target_m.linearlist[i].bias.data - calibrate_bias

        return adapted_m
                        
                    
    def eval(
        self,
        adapted_model,
        n_test,
    ):
        set_seed()
        
        adapted_model.eval()
        
        # generate random input from some Gaussian distribution
        x_test = torch.randn(n_test, self.width) 
        y_test = self.target_m(x_test).detach()
        y_test.requires_grad = False
            
        y_pred = adapted_model(x_test)
        loss = self.criterion(y_pred, y_test)
        
        self.test_loss = loss.item()
        
        if self.wandb:
            wandb.log({'test_loss': self.test_loss})
        else:
            print(f"Test loss: {self.test_loss:.4f}")
    
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--width', type=int, default=4)
    parser.add_argument('--target_depth', type=int, default=1)
    parser.add_argument('--frozen_depth', type=int, default=2)
    parser.add_argument('--rank', type=int, default=2)
    parser.add_argument('--use_bias', type=int, default=1, choices = [0,1])
    parser.add_argument('--activation', type=str, default='relu', choices = ['relu', 'linear'])
    parser.add_argument('--std', type=float, default=.25)
    parser.add_argument('--method', type=str, default='ours', choices = ['ours', 'sgd'])
    parser.add_argument('--batch_size', type=int, default=5000)
    parser.add_argument('--n_epochs', type=int, default=1000)
    parser.add_argument('--lr', type=float, default=1e-3)
    parser.add_argument('--n_test', type=int, default=10000)
    parser.add_argument('--weight_decay', type=float, default=0)
    parser.add_argument('--init_mode', type=str, default='default', choices = ['default', 'uniform_singular_values'])

    parser.add_argument('--exp', type=str, default='fnn', choices = ['fnn', 'tfn'])
    parser.add_argument('--wandb', type=int, default=0, choices = [0,1])
    
    args = parser.parse_args()
    
    # print experiment configuration
    args_dict = vars(args)
    print('Experiment Setting:')
    for key, value in args_dict.items():
        print(f"| {key}: {value}")
    
    # initialize wandb
    if args.wandb:
        wandb.init(
            project = "lora-theory",
            group =  args.exp,
            entity = 'lee-lab-uw-madison',
            job_type = args.init_mode,
            config = args,
        )
    
    # run the experiment
    if args.exp == 'fnn':
        approx_fnn(
            width = args.width,
            target_depth = args.target_depth,
            frozen_depth = args.frozen_depth,
            rank = args.rank,
            use_bias = args.use_bias,
            activation = args.activation,
            std = args.std,
            method = args.method,
            batch_size = args.batch_size,
            n_epochs = args.n_epochs,
            lr = args.lr,
            n_test = args.n_test,
            weight_decay = args.weight_decay,
            log_wandb = args.wandb,
            init_mode = args.init_mode,
        )

    elif args.exp == 'tfn':
        pass

    if args.wandb:
        wandb.finish()
