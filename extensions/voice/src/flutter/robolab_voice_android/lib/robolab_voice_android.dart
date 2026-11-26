import 'dart:async';

import 'package:flet/flet.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:flutter/widgets.dart';

class Extension extends FletExtension {
  @override
  FletService? createService(Control control) {
    return control.type == 'RoboLabVoice'
        ? _RoboLabVoice(control: control)
        : null;
  }
}

class _RoboLabVoice extends FletService with WidgetsBindingObserver {
  _RoboLabVoice({required super.control});

  static const _channel = MethodChannel('io.github.resassance.robolab/voice');
  int? _session;

  @override
  void init() {
    super.init();
    WidgetsBinding.instance.addObserver(this);
    control.addInvokeMethodListener(_invoke);
    _channel.setMethodCallHandler(_receive);
  }

  Future<dynamic> _invoke(String name, dynamic args) async {
    final session = args['session'] as int;
    switch (name) {
      case 'start':
        _session = session;
        try {
          await _channel.invokeMethod<void>(name, args);
        } catch (_) {
          if (_session == session) {
            _session = null;
          }
          rethrow;
        }
        return null;
      case 'stop':
        await _stop(session);
        return null;
      default:
        throw MissingPluginException(name);
    }
  }

  Future<void> _receive(MethodCall call) async {
    final payload = Map<String, dynamic>.from(call.arguments as Map);
    if (payload['session'] != _session) {
      return;
    }
    switch (call.method) {
      case 'phrase':
        control.triggerEvent('phrase', payload);
        return;
      case 'error':
        _session = null;
        control.triggerEvent('error', payload);
        return;
    }
  }

  Future<void> _stop(int session) async {
    if (_session == session) {
      _session = null;
    }
    try {
      await _channel.invokeMethod<void>('stop', {'session': session});
    } on PlatformException catch (error) {
      debugPrint('RoboLab microphone stop: ${error.code}');
    } on MissingPluginException {
      return;
    }
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    final session = _session;
    if (session == null) {
      return;
    }
    if (state == AppLifecycleState.hidden ||
        state == AppLifecycleState.paused ||
        state == AppLifecycleState.detached) {
      unawaited(_stop(session));
      control.triggerEvent('error', {
        'session': session,
        'message':
            'Голосовое управление остановлено при сворачивании приложения.',
      });
    }
  }

  @override
  void dispose() {
    final session = _session;
    if (session != null) {
      unawaited(_stop(session));
    }
    WidgetsBinding.instance.removeObserver(this);
    control.removeInvokeMethodListener(_invoke);
    _channel.setMethodCallHandler(null);
    super.dispose();
  }
}
