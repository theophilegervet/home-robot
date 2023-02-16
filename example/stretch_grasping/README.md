# Stand-alone grasping setup

## Environment setup on server
```
# General installation
conda create -n home_robot python=3.8 && conda activate home_robot
conda install -c conda-forge mamba
pip install -e .

# Specific to stand-alone grasping script
pip install trimesh pybullet matplotlib open3d opencv-python rospkg numpy==1.21
git clone https://github.com/mjd3/tracikpy.git && pip install tracikpy
conda install pytorch==1.11.0 torchvision==0.12.0 torchaudio==0.11.0 cudatoolkit=11.3 -c pytorch
python -m pip install 'git+https://github.com/facebookresearch/detectron2.git'
```

## Launch on robot
```
# To make debugging easier
roscore

# Launch core components
roslaunch home_robot startup_stretch_hector_slam.launch
```

## Launch on server
```
# Launch GraspNet
cd ~/src/contact_graspnet && conda activate contact_graspnet_env && python contact_graspnet/graspnet_ros_server.py --local_regions --filter_grasps

# Launch rviz
roslaunch home_robot visualization.launch

# Run stand-alone grasping script
python pick_cup_real_robot.py
```

## Troubleshooting the robot
```
# To control the robot to a starting position
roslaunch home_robot controller.launch

# To reset everything after some component fails
stretch_robot_home.py
```