import 'package:desktop_drop/desktop_drop.dart';
import 'package:flet/flet.dart';
import 'package:flutter/widgets.dart';

class Extension extends FletExtension {
  @override
  Widget? createWidget(Key? key, Control control) {
    if (control.type != 'RoboLabDropZone') {
      return null;
    }

    return LayoutControl(
      key: key,
      control: control,
      child: DropTarget(
        enable: !control.disabled && control.get<bool>('accepting', true)!,
        onDragDone: (details) => control.triggerEvent('drop', {
          'paths': details.files.map((item) => item.path).toList(),
        }),
        child: control.buildWidget('content') ?? const SizedBox.shrink(),
      ),
    );
  }
}
