import pyro
import pyro.distributions as dist
from pyro.infer import SVI, Trace_ELBO
from pyro.optim import Adam
from pyro.nn import PyroModule, PyroSample
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import train_test_split
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import classification_report, accuracy_score, precision_score, recall_score, r2_score
from sklearn.utils.class_weight import compute_class_weight
from torch.utils.data import DataLoader, TensorDataset
import yfinance as yf
from sklearn.pipeline import Pipeline
from sklearn.metrics import accuracy_score
import pandas as pd
import jax.random as jr
import numpy as np
import matplotlib.pyplot as plt
import torch.nn.functional as F
import torch.nn as nn
import torch
import jax
from pyro.infer.autoguide import AutoNormal

# ------------ config --------------
SYMBOL = "BTC-USD"
START = "2015-01-01"
END = "2025-04-17"
PERIOD = "1d"
LOOKBACK = 7
BATCH_SIZE = 16
LEARNING_RATE = 0.0001
EPOCHS = 2000
device = "cuda:0" if torch.cuda.is_available() else "cpu"
# ----------------------------------

features = [
    'log_return_5',
    'momentum_3',
    'volatility',
    'volume',
    'rolling_return_5'
]

data = yf.Ticker(SYMBOL).history(period=PERIOD, start=START, end=END)[["Open", "High", "Low", "Close", "Volume"]]
data.columns = data.columns.str.lower()

for i in range(1, LOOKBACK+1):
  idx = f"close-{i}"
  data[idx] = data.close.shift(i)
  features.append(idx)

data["rolling_return_5"] = data["close"].pct_change(3)
data["log_return_5"] = np.log(data['close'] / data['close'].shift(5))
data['log_return'] = np.log(data['close'] / data['close'].shift(1))
data["volatility"] = data["log_return"].rolling(window=5).std()
data["momentum_3"] = data['close'] - data['close'].shift(5)

data['future_return'] = data['close'].shift(-5) / data['close'] - 1
threshold = 0.002
data['target'] = data['future_return'].apply(lambda x: 1 if x > threshold else 0)
#data['target'] = pd.qcut(data['future_return'], q=2, labels=[1, 0])
data.dropna(inplace=True)

x = data[features]
y = data.target
print(y.value_counts(normalize=True))

x_train, x_test, y_train, y_test = train_test_split(x, y, test_size=0.2)

y_mean = y_train.mean()
y_std = y_train.std()
y_train = (y_train - y_mean) / y_std

y_mean = y_test.mean()
y_std = y_test.std()
y_test = (y_test - y_mean) / y_std

class multinomial_lr():
  def __init__(self):
    self.pipeline = Pipeline([
        ('scaler', StandardScaler()),
        ('clf', LogisticRegression(multi_class='multinomial', solver='lbfgs'))
    ])
  def train(self):
    self.pipeline.fit(x_train, y_train)
  def eval(self):
    y_pred = self.pipeline.predict(x_test)
    accuracy = accuracy_score(y_test, y_pred)
    print("Classification Report:\n", classification_report(y_test, y_pred))
    print(f"model r2_score: {accuracy}")
  def pred(self, x):
    probs = self.pipeline.predict_proba(x)

class MCDropoutNN(nn.Module):
    def __init__(self, input_dim, hidden_dim, output_dim):
        super().__init__()
        self.fc1 = nn.Linear(input_dim, hidden_dim)
        self.drop = nn.Dropout(p=0.1 or 0.2)
        self.fc2 = nn.Linear(hidden_dim, output_dim)
        self.relu = nn.ReLU()

    def forward(self, x):
      x = self.relu(self.drop(self.fc1(x)))
      return self.fc2(x)



class TrainMCNN():
    def __init__(self, input_dim, hidden_dim, output_dim):
      weights = compute_class_weight('balanced', classes=np.unique(y_train), y=y_train)

      weights = torch.tensor(weights, dtype=torch.float32)
      loss_fn = nn.CrossEntropyLoss(weight=weights)

      model = MCDropoutNN(input_dim, hidden_dim, output_dim)
      optimizer = torch.optim.Adam(lr=LEARNING_RATE, params = model.parameters())

    def process(self, x, y):
      x_train, x_test, y_train, y_test = train_test_split(x, y, test_size=0.2)
      scaler = StandardScaler()
      x_train = scaler.fit_transform(x_train)
      x_test = scaler.transform(x_test)

      x_train = torch.tensor(x_train, dtype=torch.float32)
      x_test = torch.tensor(x_test, dtype=torch.float32)
      y_train = torch.tensor(y_train.values, dtype=torch.long)
      y_test = torch.tensor(y_test.values, dtype=torch.long)

      return x_train, x_test, y_train, y_test

    def load(self, x_train, x_test, y_train, y_test, BATCH_SIZE):
      train_dataset = TensorDataset(x_train, y_train)
      test_dataset = TensorDataset(x_test, y_test)
      train_loader = DataLoader(train_dataset, batch_size=BATCH_SIZE, shuffle=False)
      test_loader = DataLoader(test_dataset, batch_size=BATCH_SIZE, shuffle=False)
      return train_loader, test_loader

    def run(self, EPOCHS, train_loader, test_loader):
      for epoch in range(EPOCHS+1):
          model.train()
          for xb, yb in train_loader:
              optimizer.zero_grad()
              logits = model(xb)
              loss = loss_fn(logits, yb)
              loss.backward()
              optimizer.step()

          if epoch % 10 == 0:
              model.eval()
              with torch.no_grad():
                  correct, total = 0, 0
                  for xb, yb in test_loader:
                      preds = torch.argmax(model(xb), dim=1)
                      correct += (preds == yb).sum().item()
                      total += yb.size(0)
                  test_acc = correct / total

                  correct, total = 0, 0
                  for xb, yb in train_loader:
                      preds = torch.argmax(model(xb), dim=1)
                      correct += (preds == yb).sum().item()
                      total += yb.size(0)
                  train_acc = correct / total

                  print(f"Epoch {epoch} | Train Acc: {train_acc:.4f} | Test Acc: {test_acc:.4f}")

class SubNet(PyroModule):
  def __init__(self, input_dim, width):
    super().__init__()
    self.fc1 = PyroModule[nn.Linear](input_dim, width)
    self.fc1.weight = PyroSample(dist.Normal(0, .1).expand([width, input_dim]).to_event(2))
    self.fc1.bias = PyroSample(dist.Normal(0, 1).expand([width]).to_event(1))

    self.fc2 = PyroModule[nn.Linear](width, 1)
    self.fc2.weight = PyroSample(dist.Normal(0, 1).expand([1, width]).to_event(2))
    self.fc2.bias = PyroSample(dist.Normal(0, 1).expand([1]).to_event(1))

    self.relu = nn.ReLU()
  
  def forward(self, x):
    x = self.relu(self.fc1(x))
    return self.fc2(x)


class AdditiveBNN(PyroModule):
  def __init__(self, input_dim, width=20):
    super().__init__()
    self.net1 = SubNet(input_dim, width)
    self.net2 = SubNet(input_dim, width)
    self.net3 = SubNet(input_dim, width)

  def forward(self, x, y=None):
    mean = self.net1(x) + self.net2(x) + self.net3(x)
    sigma = pyro.sample("sigma", dist.Uniform(0, 1))
    with pyro.plate("data", x.shape[0]):
      obs = pyro.sample("obs", dist.Normal(mean.squeeze(-1), sigma), obs=y)
    return mean

def guide(x, y=None):
    for name, module in bnn.named_modules():
        for param_name, _ in list(module.named_parameters(recurse=False)):
            loc_name = f"{name}.{param_name}.loc"
            scale_name = f"{name}.{param_name}.scale"
            loc = pyro.param(loc_name, torch.randn_like(getattr(module, param_name)))
            scale = pyro.param(scale_name, 0.1 * torch.ones_like(getattr(module, param_name)), constraint=dist.constraints.positive)
            pyro.sample(f"{name}.{param_name}", dist.Normal(loc, scale).to_event(getattr(module, param_name).dim()))

    pyro.sample("sigma", dist.Uniform(0.1, 1.0))

scaler = StandardScaler()
x_train = scaler.fit_transform(x_train)
x_test = scaler.transform(x_test)

y_train = torch.tensor(y_train, dtype=torch.float32)
x_train = torch.tensor(x_train, dtype=torch.float32)

y_test = torch.tensor(y_test, dtype=torch.float32)
x_test = torch.tensor(x_test, dtype=torch.float32)

input_dim = x_train.shape[1]
bnn = AdditiveBNN(input_dim=input_dim, width=20)
guide = AutoNormal(bnn)

optimizer = Adam({"lr": 0.001})
svi = SVI(bnn, guide, optimizer, loss=Trace_ELBO())

for step in range(EPOCHS+1):
  loss = svi.step(x_train, y_train)
  if step % 100 == 0:
    print(f"[{step}] ELBO loss: {loss:.4f}")


predictive = Predictive(bnn, guide=guide, num_samples=1000)
samples = predictive(x_test)

y_preds = samples["obs"].detach().numpy()

y_mean = y_preds.mean(axis=0)
y_std = y_preds.std(axis=0)

# y_mean = y_mean * y_train_std + y_train_mean
# y_std = y_std * y_train_std


plt.figure(figsize=(10, 5))
plt.plot(y_test.numpy(), label='True')
plt.plot(y_mean, label='Predicted Mean')
plt.fill_between(range(len(y_mean)), y_mean - 2*y_std, y_mean + 2*y_std,
                 alpha=0.3, label='95% CI')
plt.legend()
plt.title("BNN Prediction with Pyro")
plt.show()
