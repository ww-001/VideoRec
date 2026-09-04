# -*- coding: utf-8 -*-
"""Project 管理：一个实验 = 一个 project。

存：
  - project.json       配置（环境备注、触发区域、阈值）
  - reference.png      参考帧
  - reference_camera.json  参考时的摄像头参数快照
  - recordings/        录像输出目录
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional

from .trigger import TriggerRegion


@dataclass
class Project:
    """实验项目配置。"""

    name: str = ""
    created_at: float = 0.0
    notes: str = ""                    # 环境备注（相机高度/角度/距离、灯光位置等）
    experiment_info: str = ""          # 实验信息（动物/组别/处理/日期等，可空）
    apparatus: str = ""                # 所用装置（相机型号/镜头/固定方式等，可空）
    camera_index: int = 0
    resolution: List[int] = field(default_factory=lambda: [640, 480])
    fps: float = 30.0
    brightness_threshold_pct: float = 10.0   # 亮度差异警告阈值
    hist_corr_threshold: float = 0.9         # 直方图相关性阈值
    trigger: Optional[TriggerRegion] = None  # 鼠标触发区域
    trigger_delay_ms: int = 0                # 触发后延迟开始录制的毫秒数
    recordings_dir: str = "recordings"       # 录像输出子目录名
    project_dir: str = ""                    # 项目保存路径（绝对路径，必填）

    def to_dict(self) -> dict:
        d = asdict(self)
        d["trigger"] = self.trigger.to_dict() if self.trigger else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Project":
        p = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        if d.get("trigger"):
            p.trigger = TriggerRegion.from_dict(d["trigger"])
        return p


class ProjectManager:
    """project 文件的读写。"""

    def __init__(self, root_dir: str | Path):
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def create(self, name: str, **kwargs) -> Project:
        p = Project(name=name, created_at=time.time(), **kwargs)
        # 未指定项目路径时，默认放在 root_dir 下
        if not p.project_dir:
            p.project_dir = str(self.path_of(p))
        self.save(p)
        return p

    def path_of(self, project: Project) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in project.name)
        return self.root_dir / safe

    def save(self, project: Project) -> Path:
        # 项目路径：优先用 project.project_dir，否则退回 root_dir/name
        if project.project_dir:
            d = Path(project.project_dir)
        else:
            d = self.path_of(project)
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "project.json", "w", encoding="utf-8") as f:
            json.dump(project.to_dict(), f, ensure_ascii=False, indent=2)
        return d

    def list_projects(self) -> List[Path]:
        return [d for d in self.root_dir.iterdir()
                if d.is_dir() and (d / "project.json").exists()]

    def load(self, dir_path: str | Path) -> Optional[Project]:
        p = Path(dir_path) / "project.json"
        if not p.exists():
            return None
        with open(p, "r", encoding="utf-8") as f:
            return Project.from_dict(json.load(f))

    def recordings_dir(self, project: Project) -> Path:
        base = Path(project.project_dir) if project.project_dir else self.path_of(project)
        return base / project.recordings_dir
