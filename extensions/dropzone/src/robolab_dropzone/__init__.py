from dataclasses import dataclass

import flet as ft


@dataclass
class DropEvent(ft.Event["DropZone"]):
    paths: list[str]


@ft.control("RoboLabDropZone")
class DropZone(ft.LayoutControl):
    content: ft.Control
    accepting: bool = True
    on_drop: ft.EventHandler[DropEvent] | None = None
