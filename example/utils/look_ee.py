import rospy
import timeit
import numpy as np

from home_robot.hw.ros.stretch_ros import HelloStretchROSInterface
from home_robot.agent.motion.robot import STRETCH_HOME_Q, HelloStretchIdx


if __name__ == "__main__":
    # Create the robot
    print("--------------")
    print("Start example - hardware using ROS")
    rospy.init_node("hello_stretch_ros_test")
    print("Create ROS interface")
    rob = HelloStretchROSInterface(
        visualize_planner=False,
    )
    print("Wait...")
    rospy.sleep(0.5)  # Make sure we have time to get ROS messages
    for i in range(1):
        q = rob.update()
        print(rob.get_base_pose())
    print("--------------")
    print("We have updated the robot state. Now test goto.")

    home_q = STRETCH_HOME_Q
    model = rob.get_model()
    q = model.update_look_at_ee(home_q.copy())
    rob.goto(q, move_base=False, wait=True)