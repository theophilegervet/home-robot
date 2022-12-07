import gzip
import json
import glob
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


data_dir = "home_robot/experimental/theo/habitat_projects/datasets/episode_datasets/objectgoal_floorplanner/v1/train/content"
scene_dataset = "home_robot/experimental/theo/habitat_projects/datasets/scene_datasets/floorplanner/v1/hab-fp.scene_dataset_config.json"


for path in glob.glob(f"{data_dir}/*.gz"):
    print(path)

    with gzip.open(path, "rt") as f:
        scene_data = json.load(f)

    for episode in scene_data["episodes"]:
        episode["scene_dataset_config"] = scene_dataset

    with gzip.open(path, "w") as f:
        f.write(json.dumps(scene_data).encode())