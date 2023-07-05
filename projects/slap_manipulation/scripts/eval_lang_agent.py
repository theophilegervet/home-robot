# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.


import click
import numpy as np
import rospy
from slap_manipulation.agents.general_language_agent import GeneralLanguageAgent
from slap_manipulation.env.general_language_env import GeneralLanguageEnv

from home_robot_hw.utils.config import load_slap_config


@click.command()
@click.option("--test-pick", default=False, is_flag=True)
@click.option("--dry-run", default=False, is_flag=True)
@click.option("--testing/--no-testing", default=False, is_flag=True)
@click.option("--object", default="cup")
@click.option("--task-id", default=0)
@click.option(
    "--cat-map-file", default="projects/stretch_ovmm/configs/example_cat_map.json"
)
@click.option("--start-from", default=0)
def main(
    task_id,
    cat_map_file,
    test_pick=False,
    dry_run=False,
    testing=False,
    start_from=0,
    **kwargs
):
    TASK = task_id
    # TODO: add logic here to read task_id automatically for experiments
    rospy.init_node("eval_episode_lang_ovmm")

    config = load_slap_config(
        visualize=True,
        config_path="projects/slap_manipulation/configs/language_agent.yaml",
        **kwargs
    )

    env = GeneralLanguageEnv(
        config=config,
        test_grasping=test_pick,
        dry_run=dry_run,
        # segmentation_method="detic",
        cat_map_file=cat_map_file,
    )
    agent = GeneralLanguageAgent(
        cfg=config,
        debug=True,
        task_id=task_id,
        skip_gaze=True,
        start_from=start_from,
    )
    # robot = env.get_robot()

    agent.reset()
    env.reset()

    center = input("Press Y to send robot back to 0,0,0")
    if center == "y" or center == "Y":
        env.robot.nav.navigate_to(np.zeros(3), relative=False, blocking=True)
    t = 0
    grip = input("Y to close gripper")
    if grip == "y" or grip == "Y":
        env.robot.switch_to_manipulation_mode()
        env.robot.manip.close_gripper()
        env.robot.switch_to_navigation_mode()
    else:
        env.robot.switch_to_manipulation_mode()
        env.robot.manip.open_gripper()
        env.robot.switch_to_navigation_mode()

    while not agent.task_is_done():
        t += 1
        print("TIMESTEP = ", t)
        obs = env.get_observation()
        action, info = agent.act(obs, TASK)
        print("ACTION = ", action)
        # input("Press enter to apply this action")
        env.apply_action(action, info=info)


if __name__ == "__main__":
    main()
