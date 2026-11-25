import flet as ft

BACKGROUND = "#F3F6FA"
SURFACE = "#FFFFFF"
SUBTLE = "#EAF0F6"
LINE = "#CCD5E2"
OUTLINE = "#748398"
TEXT = "#162235"
MUTED = "#475A72"
PRIMARY = "#2542BF"
PRIMARY_TINT = "#EAF0FF"
TEAL = "#0C666B"
TEAL_TINT = "#E4F4F3"
AMBER = "#86510A"
AMBER_TINT = "#FFF1DB"
SUCCESS = "#16643D"
SUCCESS_TINT = "#E7F5EC"
ERROR = "#9A231B"
ERROR_TINT = "#FFEDE9"
DANGER = "#B42318"

CONTROL_RADIUS = 6


def light_theme() -> ft.Theme:
    buttons = ft.ButtonStyle(
        shape=ft.RoundedRectangleBorder(radius=CONTROL_RADIUS),
        padding=ft.Padding.symmetric(horizontal=10),
    )
    return ft.Theme(
        color_scheme=ft.ColorScheme(
            primary=PRIMARY,
            on_primary=SURFACE,
            primary_container=PRIMARY_TINT,
            on_primary_container=PRIMARY,
            secondary=TEAL,
            on_secondary=SURFACE,
            secondary_container=TEAL_TINT,
            on_secondary_container=TEAL,
            tertiary=AMBER,
            on_tertiary=SURFACE,
            tertiary_container=AMBER_TINT,
            on_tertiary_container=AMBER,
            surface=SURFACE,
            on_surface=TEXT,
            on_surface_variant=MUTED,
            outline=OUTLINE,
            outline_variant=LINE,
            surface_container_high=SUBTLE,
            error=ERROR,
            error_container=ERROR_TINT,
            on_error_container=ERROR,
        ),
        divider_color=LINE,
        input_decoration_theme=ft.InputDecorationTheme(
            border=ft.OutlineInputBorder(
                border_radius=CONTROL_RADIUS, side=ft.BorderSide(1, OUTLINE)
            ),
        ),
        text_theme=ft.TextTheme(body_medium=ft.TextStyle(size=15, color=TEXT)),
        button_theme=ft.ButtonTheme(style=buttons),
        filled_button_theme=ft.FilledButtonTheme(style=buttons),
        outlined_button_theme=ft.OutlinedButtonTheme(style=buttons),
        text_button_theme=ft.TextButtonTheme(style=buttons),
        icon_button_theme=ft.IconButtonTheme(style=buttons),
        segmented_button_theme=ft.SegmentedButtonTheme(style=buttons),
        dialog_theme=ft.DialogTheme(
            bgcolor=SURFACE,
            shape=ft.RoundedRectangleBorder(radius=10),
            title_text_style=ft.TextStyle(size=22, color=TEXT, weight=ft.FontWeight.W_700),
        ),
    )


def button_row(*controls: ft.Control, spacing: float = 8) -> ft.Row:
    for control in controls:
        control.expand = 1
    return ft.Row(list(controls), spacing=spacing)
