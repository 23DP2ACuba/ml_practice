'''
RecurrentPPO for stock trading
'''

from sb3_contrib.ppo_recurrent.policies import RecurrentActorCriticPolicy
from stable_baselines3.common.vec_env import DummyVecEnv
from sklearn.preprocessing import StandardScaler
from stable_baselines3 import PPO, A2C, DQN
from sb3_contrib import RecurrentPPO
import matplotlib.pyplot as plt
import gymnasium as gym
import gym_trading_env
from sys import exit
import numpy as np
import hmmlearn
import yfinance
import warnings
import pickle
import os
warnings.filterwarnings("ignore", category=ImportWarning)

os.environ["TF_ENABLE_ONEDNN_OPTS"] = "0"

# ------------------- Configuration -------------------

SYMBOL = 'TSLA'
folder = "TSLA"
START_DATE = '2020-01-01'
END_DATE = '2025-06-27'
PERIOD = "1d"
WINDOW_SIZE = 30
TIMESTEPS = 10_000
TRADING_FEES = 0.002
model_type = "RecurrentPPO"
# -----------------------------------------------------

data = yfinance.Ticker(SYMBOL).history(period=PERIOD, start=START_DATE, end=END_DATE)
data = data[["Open", "High", "Low", "Close", "Volume"]]

def create_features(data):
    """Create features with proper lagging"""
    df = data.copy()
    df['return'] = df['Close'].pct_change().shift(1)
    df['volatility'] = df['return'].rolling(5, min_periods=1).std().shift(1)
    df['momentum'] = (df['Close'].shift(1) - df['Close'].shift(6))
    df['log_volume'] = np.log(df['Volume'].shift(1) + 1e-6)
    return df.dropna()

processed_data = create_features(data)
prices = processed_data.Close.values

try:
    with open(f"hmm_model_{folder}.pkl", "rb") as f:
        hmm_model = pickle.load(f)
except FileNotFoundError as e:
    print(f"Model loading failed: {e}")
    exit()

train_data = processed_data.loc["2022-01-01":"2024-05-30"].copy()
test_data = processed_data.loc["2024-06-01":"2025-07-27"].copy()
features = ["return", "volatility", "momentum", "log_volume"]

scaler = StandardScaler()
train_X = scaler.fit_transform(train_data[features])
test_X = scaler.transform(test_data[features])

def proper_rolling_predict(model, data, window_size=30):
    predictions = []
    for i in range(window_size, len(data)):
        window = data[i-window_size:i]
        try:
            state = model.predict(window)[-1]
        except Exception as e:
            print(f"Prediction failed at step {i}: {e}")
            state = 0
        predictions.append(state)
    return [predictions[0]] * window_size + predictions

train_data['hidden_state'] = proper_rolling_predict(hmm_model, train_X)
test_data['hidden_state'] = proper_rolling_predict(hmm_model, test_X)

train_data.columns = [col.lower() for col in train_data.columns]
test_data.columns = [col.lower() for col in test_data.columns]

print("Preprocessing finished")

env_maker = lambda: gym.make("TradingEnv", df=train_data, trading_fees=TRADING_FEES)
env = DummyVecEnv([env_maker])


model = RecurrentPPO(
      policy=RecurrentActorCriticPolicy,
      env=env,
      verbose=1,
      n_steps=2048,
      batch_size=64,
      learning_rate=1e-2,
      policy_kwargs=dict(
          lstm_hidden_size=128,    # LSTM layer size
          net_arch=[64],           # Fully connected layer before LSTM
          ortho_init=True
      )
  )

print(f"Model defined: {model_type}")
model.learn(total_timesteps=TIMESTEPS)
print("Learning finished")

model.save(f"RLxHMM_model/{folder}/{model_type}_model")



print(f"Model saved: {folder}/{model_type}_model")


test_env = gym.make("TradingEnv", df=test_data, trading_fees=TRADING_FEES)
obs, info = test_env.reset()
done = False
total_reward = 0
dates = []
portfolio_values = []
stock_prices = []

while not done:
    action, _ = model.predict(obs)
    obs, reward, terminated, truncated, info = test_env.step(action)
    dates.append(info['date'])
    portfolio_values.append(info['portfolio_valuation'])
    stock_prices.append(info['data_close'])
    total_reward += reward
    done = terminated or truncated
    #print(f"Step Date: {info['date']} | Portfolio: {info['portfolio_valuation']:.2f} | Stock: {info['data_close']:.2f} | Reward: {reward}")


print("Final Test Metrics:", {"total_reward": total_reward, **info})

initial_price = stock_prices[0]
initial_investment = 1000
shares = initial_investment / initial_price
buy_and_hold_values = [shares * sp for sp in stock_prices]

plt.figure(figsize=(12, 6))
plt.plot(dates, portfolio_values, label='RL Portfolio')
plt.plot(dates, buy_and_hold_values, label='Buy and Hold')
plt.xlabel('Date')
plt.ylabel('Portfolio Value')
plt.title('RL Portfolio vs Buy and Hold')
plt.legend()
plt.show()
