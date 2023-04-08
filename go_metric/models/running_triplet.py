import torch
import torch.nn as nn
import numpy as np
import pytorch_lightning as pl
from sklearn.metrics import f1_score
from go_metric.utils import tuple_type
from go_metric.metric_loss import multilabel_triplet_loss
from go_metric.multilabel_knn import embedding_knn
from scipy import sparse

def get_MLP(layer_dim, dropout=0.5):
    mlp = nn.Sequential()
    for i in range(len(layer_dim)-1):
        mlp.append(nn.Linear(layer_dim[i], layer_dim[i+1]))
        if(i < len(layer_dim)-2):
            mlp.append(nn.Dropout(p=dropout))
            mlp.append(nn.ReLU())
    return mlp

class EmbedMetric(nn.Module):
    def __init__(self, emb_model, bottleneck_dim, hidden_dims, num_classes, nb_filters, max_kernel, max_len):
        super().__init__()
        self.emb_model = emb_model
        self.l2 = nn.Linear(bottleneck_dim, 1024)
        self.classifier_layer = nn.Linear(1024, num_classes)
        self.relu = nn.ReLU()

    def forward(self, x, return_embedding=False):
        # out = self.embedding(x)
        embedding = self.emb_model(x)
        out = self.relu(embedding)
        out = self.l2(out)
        out = self.relu(out)
        out = self.classifier_layer(out)
        if(return_embedding):
            return out, embedding
        return out

    def embedding(self, x):
        return self.emb_model(x)

class EmbMetricModule(pl.LightningModule):
    def __init__(self, vocab_size=30, num_classes=865, max_len=1024, 
                    max_kernel=129, num_filters=512, bottleneck_dim=128, hidden_dims=(2048, 2048), bottleneck_regularization=0.0, 
                    bottleneck_layers=1, classification_layers=1, label_loss_weight=10.0, label_loss_decay=0.94,
                    sim_margin=3.0, tmargin=1.0, sim_type='dot', gradient_clipping_decay=1.0, batch_size=32,
                    learning_rate=5e-4, lr_decay_rate= 0.9997, running_batches=16, 
                    term_ic=None, git_hash=None):
        super().__init__()
        self.save_hyperparameters()
        self.term_ic = term_ic
        self.model = EmbedMetric(vocab_size, bottleneck_dim, list(hidden_dims), num_classes, num_filters, max_kernel, max_len)
        self.lr = learning_rate
        self.sim_type = sim_type
        self.sim_margin = sim_margin
        self.tmargin = tmargin
        self.bottleneck_regularization = bottleneck_regularization
        self.label_loss_weight = label_loss_weight
        self.label_loss_decay = label_loss_decay
        self.running_batches = running_batches
        self.batch_place = 0
        self.emb_record = torch.zeros(batch_size*running_batches, bottleneck_dim)
        self.label_record = torch.zeros(batch_size*running_batches, num_classes, dtype=torch.long)
        self.loss = nn.BCEWithLogitsLoss()

    def forward(self, x, return_embedding=False):
        return self.model(x, return_embedding)

    def on_epoch_start(self):
        print('\n')

    def training_step(self, batch, batch_idx):
        # training_step defined the train loop.
        x, y = batch["seq"], batch["labels"] 
        y = y.float()
        logits, embedding = self.model.forward(x, return_embedding=True)
        label_loss = self.loss(logits, y)
        term_ic = self.term_ic.to(x.device)
        metric_loss, num_triplets = multilabel_triplet_loss(embedding, y, label_weights=term_ic, sim_margin=self.sim_margin, tmargin=self.tmargin, sim_type=self.sim_type)
        emb_loss = self.bottleneck_regularization * torch.square(embedding).sum(axis=1).mean()
        loss = self.label_loss_weight*label_loss + metric_loss + emb_loss
        # Logging to TensorBoard by default
        self.log('loss/train', loss)
        self.log('label_loss/train', label_loss)
        self.log('metric_loss/train', metric_loss)
        self.log('batch_triplets', num_triplets)
        return {"loss": loss, "embeddings":embedding.detach(), "labels": sparse.csr_matrix(batch["labels"].cpu().numpy())}
    
    def training_epoch_end(self, outputs):
        labels, embeddings = [], []
        for output in outputs:
            labels.append(output["labels"])
            embeddings.append(output["embeddings"])
        self.train_labels = sparse.vstack(labels)
        self.train_embeddings = torch.cat(embeddings, dim=0)

        train_embeddings = self.train_embeddings.to(self.device)
        val_embeddings = self.val_embeddings.to(self.device)
        val_preds = embedding_knn(train_embeddings, val_embeddings, self.train_labels, k=3).toarray() >= 0.3
        knn_f1 = f1_score(self.val_labels, val_preds, average='micro')
        self.log('knn_F1/val', knn_f1, prog_bar=True)
        self.train_embeddings, self.train_labels, self.val_embeddings, self.val_labels = None, None, None, None
        self.label_loss_weight *= self.label_loss_decay
        return super().training_epoch_end(outputs)
        
    def validation_step(self, batch, batch_idx):
        # training_step defined the train loop.
        x, y = batch["seq"], batch["labels"] 
        y = y.float()
        logits, embedding = self.model.forward(x, return_embedding=True)
        label_loss = self.loss(logits, y)
        metric_loss, num_triplets = multilabel_triplet_loss(embedding, y, label_weights=self.term_ic, sim_margin=3.0, tmargin=1.0)
        loss = label_loss + metric_loss
        # Logging to TensorBoard by default
        self.log('loss/val', loss)
        self.log('label_loss/val', label_loss)
        self.log('metric_loss/val', metric_loss)
        output = {"loss": loss, "label_loss": label_loss, "metric_loss": metric_loss, 
                "labels": batch["labels"], "logits": logits, "embeddings": embedding}
        return output

    def validation_epoch_end(self, outputs):
        labels, logits, embeddings = [], [], []
        for output in outputs:
            labels.append(output["labels"])
            logits.append(output["logits"])
            embeddings.append(output["embeddings"])
        labels = torch.cat(labels, dim=0).cpu().numpy()
        logits = torch.cat(logits, dim=0)

        preds = (logits > 0).cpu().numpy()
        f1 = f1_score(labels, preds, average='micro')
        self.log('F1/val', f1, prog_bar=True)

        val_embeddings = torch.cat(embeddings, dim=0)
        self.val_embeddings = val_embeddings
        self.val_labels = labels
        return super().validation_epoch_end(outputs)

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), lr=self.lr, weight_decay=1e-7)
        # scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode='max', factor=0.3, patience=5)
        return optimizer
    
    def on_train_start(self):
        # Proper logging of hyperparams and metrics in TB
        self.logger.log_hyperparams(self.hparams, {"loss/train": 1, "loss/val": 1})

    @staticmethod
    def add_model_specific_args(parent_parser):
        parser = parent_parser.add_argument_group("LitModel")
        parser.add_argument('--vocab_size', type=int, default=30)
        parser.add_argument('--batch_size', type=int, default=256)
        parser.add_argument('--max_len', type=int, default=1024)

        parser.add_argument('--num_classes', type=int, default=865)
        parser.add_argument('--num_filters', type=int, default=800)
        parser.add_argument('--max_kernel', type=int, default=129)
        parser.add_argument("--bottleneck_dim", type=int, default=128)
        parser.add_argument("--bottleneck_regularization", type=float, default=0.01)

        # parser.add_argument("--bottleneck_layers", type=int, default=1)
        parser.add_argument("--hidden_dims", type=tuple_type, default=(2048, 2048))
        parser.add_argument("--classification_layers", type=int, default=1)

        parser.add_argument('--sim_type', type=str, default='dot')
        parser.add_argument('--sim_margin', type=float, default=3.0)
        parser.add_argument('--tmargin', type=float, default=0.95)

        parser.add_argument('--label_loss_weight', type=float, default=10.0)
        parser.add_argument("--label_loss_decay", type=float, default=1.0)
        parser.add_argument('--learning_rate', type=float, default=5e-4)
        parser.add_argument('--lr_decay_rate', type=float, default=0.9997)

        parser.add_argument('--gradient_clipping_decay', type=float, default=1.0)
        
        return parent_parser

