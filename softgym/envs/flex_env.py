import os
import copy
from statistics import mode
from gym import error
import numpy as np
import gym
from softgym.utils.visualization import save_numpy_as_gif
import cv2
import os.path as osp
import pickle

try:
    import pyflex
except ImportError as e:
    raise error.DependencyNotInstalled("{}. (You need to first compile the python binding)".format(e))

from matplotlib import pyplot as plt

class FlexEnv(gym.Env):
    def __init__(self,
                 device_id=-1,
                 headless=False,
                 render=True,
                 control_horizon=100,
                 camera_width=720,
                 camera_height=720,
                 num_variations=1,
                 action_repeat=8,
                 camera_name='default_camera',
                 use_cached_states=False,
                 observation_mode = 'rgbd',
                 save_step_info=False,
                 save_image_dim=(256, 256),
                 save_cached_states=True, **kwargs):

        
        self.camera_params, self.camera_width, self.camera_height, self.camera_name = {}, camera_width, camera_height, camera_name
        pyflex.init(headless, render, camera_width, camera_height)

        self.record_video, self.video_path, self.video_name = False, None, None

        self.metadata = {'render.modes': ['human', 'rgb_array', 'rgbd']}

        if device_id == -1 and 'gpu_id' in os.environ:
            device_id = int(os.environ['gpu_id'])
        self.device_id = device_id

        self.save_step_info = save_step_info
        self.save_image_dim = save_image_dim

        self.sampling_random_state = np.random.RandomState(kwargs['random_seed'])
        self.control_horizon = control_horizon
        self.control_step = 0
        self._render = render


        self.action_repeat = action_repeat
        self.recording = False
        self.prev_reward = None
        self.use_cached_states = use_cached_states
        self.save_cached_states = save_cached_states
        self.current_config = self.get_default_config()
        self.current_config_id = None
        self.cached_configs, self.cached_init_states = None, None
        self.num_variations = num_variations

        self.dim_position = 4
        self.dim_velocity = 3
        self.dim_shape_state = 14
        self.particle_num = 0
        self.eval_flag = False
        self.observation_mode = observation_mode

        self.version = 1

    def get_cached_configs_and_states(self, cached_states_path, num_variations):
        """
        If the path exists, load from it. Should be a list of (config, states)
        :param cached_states_path:
        :return:
        """
        if self.cached_configs is not None and self.cached_init_states is not None and len(self.cached_configs) == num_variations:
            return self.cached_configs, self.cached_init_states
        if not cached_states_path.startswith('/'):
            cur_dir = osp.dirname(osp.abspath(__file__))
            cached_states_path = osp.join(cur_dir, '../cached_initial_states', cached_states_path)
        if self.use_cached_states and osp.exists(cached_states_path):
            # Load from cached file
            with open(cached_states_path, "rb") as handle:
                self.cached_configs, self.cached_init_states = pickle.load(handle)
            print('{} config and state pairs loaded from {}'.format(len(self.cached_init_states), cached_states_path))
            if len(self.cached_configs) >= num_variations:
                return self.cached_configs, self.cached_init_states

        self.cached_configs, self.cached_init_states = self.generate_env_variation(num_variations)
        if self.save_cached_states:
            with open(cached_states_path, 'wb') as handle:
                pickle.dump((self.cached_configs, self.cached_init_states), handle, protocol=pickle.HIGHEST_PROTOCOL)
            print('{} config and state pairs generated and saved to {}'.format(len(self.cached_init_states), cached_states_path))

        return self.cached_configs, self.cached_init_states

    def get_current_config(self):
        return self.current_config

    def tick_control_step(self):
        pyflex.step()
        if self._render:
            pyflex.render()
        self.control_step += 1

        if self.save_step_info:

            #print('tick control action shape', np.zeros(self.step_info['control_signal'][-1].shape).shape)
            self.step_info['control_signal'].append(np.zeros(self.step_info['control_signal'][-1].shape))
            self.step_info['picker_pos'].append(self.action_tool.get_picker_pos())
            self.step_info['particle_pos'].append(self.get_particle_pos())

    def update_camera(self, camera_name, camera_param=None):
        """
        :param camera_name: The camera_name to switch to
        :param camera_param: None if only switching cameras. Otherwise, should be a dictionary
        :return:
        """
        if camera_param is not None:
            self.camera_params[camera_name] = camera_param
        else:
            camera_param = self.camera_params[camera_name]
        pyflex.set_camera_params(
            np.array([*camera_param['pos'], *camera_param['angle'], camera_param['width'], camera_param['height']]))

    def get_state(self):
        pos = pyflex.get_positions()
        vel = pyflex.get_velocities()
        shape_pos = pyflex.get_shape_states()
        phase = pyflex.get_phases()
        camera_params = copy.deepcopy(self.camera_params)
        return {'particle_pos': pos, 'particle_vel': vel, 'shape_pos': shape_pos, 'phase': phase, 'camera_params': camera_params,
                'config_id': self.current_config_id}

    def set_state(self, state_dict):
        pyflex.set_positions(state_dict['particle_pos'])
        pyflex.set_velocities(state_dict['particle_vel'])
        pyflex.set_shape_states(state_dict['shape_pos'])
        pyflex.set_phases(state_dict['phase'])
        self.camera_params = copy.deepcopy(state_dict['camera_params'])
        self.update_camera(self.camera_name)

    def close(self):
        pyflex.clean()

    def get_colors(self):
        '''
        Overload the group parameters as colors also
        '''
        groups = pyflex.get_groups()
        return groups

    def set_colors(self, colors):
        pyflex.set_groups(colors)

    def start_record(self):
        self.video_frames = []
        self.recording = True

    def end_record(self, video_path=None, **kwargs):
        if not self.recording:
            print('function end_record: Error! Not recording video')
        self.recording = False
        if video_path is not None:
            save_numpy_as_gif(np.array(self.video_frames), video_path, **kwargs)
        del self.video_frames

    def reset(self, episode_id=None):
     
        
        config_id = episode_id
        if episode_id is None: ## if episode id is not given, we need to randomly start and episode.
            if self.eval_flag:
                eval_beg = int(0.1 * len(self.cached_configs))
                config_id = self.sampling_random_state.randint(low=0,  high=eval_beg)
            else:
                train_low = int(0.1 * len(self.cached_configs))
                config_id =  self.sampling_random_state.randint(low=train_low, high=len(self.cached_configs))
        elif not self.eval_flag:  ## if episode id is given, we need to find the config id
            config_id = episode_id + int(0.1 * len(self.cached_configs))

        print('start {} episode {}, config id {}'.\
            format(('eval' if self.eval_flag else 'train'), episode_id, config_id))
            
        self.current_config = self.cached_configs[config_id]
        self.current_config_id = config_id
        self.set_scene(self.cached_configs[config_id], self.cached_init_states[config_id])
         
        
        self.particle_num = pyflex.get_n_particles()
        self.prev_reward = 0.
        self.control_step = 0

        obs, reward = self._reset()

        if self.recording:
            self.video_frames.append(self.render(mode='rgb'))

        return obs, reward

    def step(self, action, img_size=None):
        """ If record_continuous_video is set to True, will record an image for each sub-step"""
        for i in range(self.action_repeat):
            if self.save_step_info: 
                self.step_info = {}
                self.action_tool.clean_step_info()

            self._step(action)
        obs = self._get_obs()
        reward = self.compute_reward()
        #info = self._get_info()


        done = False
        if (self.control_horizon is not None) and (self.control_step >= self.control_horizon):
            done = True
        
        if self.action_mode != 'velocity_control' and (self.action_horizon is not None) and self.action_step >= self.action_horizon:
            done = True

        # if record_continuous_video:
        #     info['flex_env_recorded_frames'] = frames

        return obs, reward, done

    def initialize_camera(self):
        """
        This function sets the postion and orientation of the camera
        camera_pos: np.ndarray (3x1). (x,y,z) coordinate of the camera
        camera_angle: np.ndarray (3x1). (x,y,z) angle of the camera (in degree).

        Note: to set camera, you need
        1) implement this function in your environement, set value of self.camera_pos and self.camera_angle.
        2) add the self.camera_pos and self.camera_angle to your scene parameters,
            and pass it when initializing your scene.
        3) implement the CenterCamera function in your scene.h file.
        Pls see a sample usage in pour_water.py and softgym_PourWater.h

        if you do not want to set the camera, you can just not implement CenterCamera in your scene.h file,
        and pass no camera params to your scene.
        """
        raise NotImplementedError

    def render(self, mode='rgb'):
        #pyflex.step()
        img, depth_img = pyflex.render()
        width, height = self.camera_params['default_camera']['width'], self.camera_params['default_camera']['height']
        img = img.reshape(height, width, 4)[::-1, :, :3]  # Need to reverse the height dimension
        depth_img = depth_img.reshape(height, width, 1)[::-1, :, :1]

        #print('depth_img', depth_img[0][0])

        if mode == 'rgbd':
            return np.concatenate((img, depth_img), axis=2)
        elif mode == 'rgb':
            return img
        elif mode == 'd':
            return depth_img
        else:
            raise NotImplementedError

    def get_image(self, width=720, height=720, depth=False):
        """ use pyflex.render to get a rendered image. """
        img = self.render(mode='rgb' + ("d" if depth else ""))
        #img = img.astype(np.uint8)
        if width != img.shape[0] or height != img.shape[1]:
            img = cv2.resize(img, (width, height))
        return img

    def set_scene(self, config, state=None):
        """ Set up the flex scene """
        raise NotImplementedError

    def get_default_config(self):
        """ Generate the default config of the environment scenes"""
        raise NotImplementedError

    def generate_env_variation(self, num_variations, **kwargs):
        """
        Generate a list of configs and states
        :return:
        """
        raise NotImplementedError

    def compute_reward(self, action=None, obs=None, set_prev_reward=False):
        """ set_prev_reward is used for calculate delta rewards"""
        raise NotImplementedError

    def _get_obs(self):
        raise NotImplementedError

    def _get_info(self):
        raise NotImplementedError

    def _reset(self):
        raise NotImplementedError

    def _step(self, action):
        raise NotImplementedError

    def _seed(self):
        pass
