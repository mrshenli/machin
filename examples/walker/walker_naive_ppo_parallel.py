import torch as t
import torch.nn as nn

from datetime import datetime as dt

from models.nets.base import StaticModuleWrapper as MW
from models.frames.algorithms.ppo import PPO
from models.naive.env_walker_ppo import Actor, Critic

from utils.logging import default_logger as logger
from utils.image import create_gif_subproc
from utils.tensor_board import global_board
from utils.helper_classes import Counter, Timer
from utils.conf import Config
from utils.save_env import SaveEnv
from utils.prep import prep_args
from utils.parallel import get_context, Pool

from env.walker.single_walker import BipedalWalker

# definitions
observe_dim = 24
action_dim = 4

# configs
c = Config()
# c.restart_from_trial = "2020_05_09_15_00_31"
c.max_episodes = 5000
c.max_steps = 1500
c.replay_size = 10000

c.device = "cuda:0"
c.root_dir = "/data/AI/tmp/multi_agent/walker/naive_ppo_parallel/"

# train configs
# lr: learning rate, int: interval
c.workers = 3
c.discount = 0.99
c.learning_rate = 3e-4
c.entropy_weight = None
c.ppo_update_times = 50
c.ppo_update_int = 6  # = the number of episodes stored in ppo replay buffer
c.model_save_int = c.ppo_update_int * 20  # in episodes
c.profile_int = 50  # in episodes


if __name__ == "__main__":
    save_env = SaveEnv(c.root_dir, restart_use_trial=c.restart_from_trial)
    prep_args(c, save_env)

    # save_env.remove_trials_older_than(diff_hour=1)
    global_board.init(save_env.get_trial_train_log_dir())
    writer = global_board.writer
    logger.info("Directories prepared.")

    actor = MW(Actor(observe_dim, action_dim, 0.5).to(c.device), c.device, c.device)
    critic = MW(Critic(observe_dim).to(c.device), c.device, c.device)
    actor.share_memory()
    critic.share_memory()
    logger.info("Networks created")

    # default replay buffer storage is main cpu mem
    # when stored in main mem, takes about 0.65e-3 sec to move result from gpu to cpu,
    ppo = PPO(actor, critic,
              t.optim.Adam, nn.MSELoss(reduction='sum'),
              replay_device=c.device,
              replay_size=c.replay_size,
              entropy_weight=c.entropy_weight,
              discount=c.discount,
              update_times=c.ppo_update_times,
              learning_rate=c.learning_rate)

    if c.restart_from_trial is not None:
        ppo.load(save_env.get_trial_model_dir())
    logger.info("PPO framework initialized")

    # training
    # preparations
    ctx = get_context("spawn")
    pool = Pool(processes=c.workers, context=ctx)
    pool.enable_global_find(True)

    # begin training
    episode = Counter(step=c.ppo_update_int)
    timer = Timer()

    while episode < c.max_episodes:
        first_episode = episode.get()
        episode.count()
        last_episode = episode.get() - 1
        logger.info("Begin episode {}-{} at {}".format(first_episode, last_episode,
                                                       dt.now().strftime("%m/%d-%H:%M:%S")))

        # begin trials
        def run_trial(episode_num):
            env = BipedalWalker()

            # render configuration
            if episode_num % c.profile_int == 0:
                render = True
            else:
                render = False
            frames = []

            # batch size = 1
            total_reward = 0
            state, reward = t.tensor(env.reset(), dtype=t.float32, device=c.device), 0

            tmp_observe = []
            local_step = Counter()
            episode_finished = False

            while not episode_finished and local_step.get() <= c.max_steps:
                local_step.count()
                timer.begin()
                with t.no_grad():
                    old_state = state

                    # agent model inference
                    action, prob, *_ = ppo.act({"state": state.unsqueeze(0)})

                    state, reward, episode_finished, _ = env.step(action[0].to("cpu"))

                    if render:
                        frames.append(env.render(mode="rgb_array"))

                    state = t.tensor(state, dtype=t.float32, device=c.device)

                    total_reward += reward

                    tmp_observe.append({"state": {"state": old_state.unsqueeze(0).clone()},
                                        "action": {"action": action.clone()},
                                        "next_state": {"state": state.unsqueeze(0).clone()},
                                        "reward": float(reward),
                                        "terminal": episode_finished or local_step.get() == c.max_steps,
                                        "action_log_prob": float(prob)
                                        })

            # ordinary sampling, calculate value for each observation
            tmp_observe[-1]["value"] = tmp_observe[-1]["reward"]
            for i in reversed(range(1, len(tmp_observe))):
                tmp_observe[i - 1]["value"] = \
                    tmp_observe[i]["value"] * c.discount + tmp_observe[i - 1]["reward"]

            return tmp_observe, total_reward, local_step.get(), frames

        results = pool.map(run_trial, range(first_episode, last_episode + 1))

        for result, episode_num in zip(results, range(first_episode, last_episode+1)):
            tmp_observe, total_reward, local_step, frames = result
            logger.info("Sum reward: {}, episode={}".format(float(total_reward), episode_num))
            writer.add_scalar("episodic_sum_reward", float(total_reward), episode_num)
            writer.add_scalar("episode_length", local_step, episode_num)

            for obsrv in tmp_observe:
                ppo.store_transition(obsrv)

            if len(frames) != 0:
                # sub-processes cannot start a sub-process
                # so we have to store results in the main process
                create_gif_subproc(frames, save_env.get_trial_image_dir() + "/{}".format(episode_num))

            # model serialization
            if episode_num % c.model_save_int == 0:
                ppo.save(save_env.get_trial_model_dir(), version=episode_num)

        logger.info("End episode {}-{} at {}".format(first_episode, last_episode,
                                                     dt.now().strftime("%m/%d-%H:%M:%S")))

        # begin training
        timer.begin()
        ppo.update()
        ppo.update_lr_scheduler()
        writer.add_scalar("train_step_time", timer.end(), episode.get())

        logger.info("Train end, time = {:.2f} s, episode={}".format(timer.end(), episode))
