package io.github.resassance.robolab_voice_android;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.pm.PackageManager;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.AudioRecordingConfiguration;
import android.media.MediaRecorder;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import io.flutter.embedding.engine.plugins.FlutterPlugin;
import io.flutter.embedding.engine.plugins.activity.ActivityAware;
import io.flutter.embedding.engine.plugins.activity.ActivityPluginBinding;
import io.flutter.plugin.common.MethodCall;
import io.flutter.plugin.common.MethodChannel;
import io.flutter.plugin.common.PluginRegistry.RequestPermissionsResultListener;
import java.io.IOException;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import org.json.JSONException;
import org.json.JSONObject;
import org.vosk.Model;
import org.vosk.Recognizer;

public final class VoicePlugin implements FlutterPlugin, ActivityAware,
        MethodChannel.MethodCallHandler, RequestPermissionsResultListener {
    private static final String CHANNEL = "io.github.resassance.robolab/voice";
    private static final int MICROPHONE_PERMISSION = 7249;
    private static final int SAMPLE_RATE = 16000;
    private static final int CHUNK_SAMPLES = SAMPLE_RATE / 10;

    private final Handler main = new Handler(Looper.getMainLooper());
    private ExecutorService worker;
    private MethodChannel channel;
    private ActivityPluginBinding activityBinding;
    private Capture current;
    private boolean permissionRequested;

    @Override
    public void onAttachedToEngine(FlutterPluginBinding binding) {
        worker = Executors.newSingleThreadExecutor();
        channel = new MethodChannel(binding.getBinaryMessenger(), CHANNEL);
        channel.setMethodCallHandler(this);
    }

    @Override
    public void onDetachedFromEngine(FlutterPluginBinding binding) {
        cancelCurrent();
        channel.setMethodCallHandler(null);
        channel = null;
        worker.shutdown();
    }

    @Override
    public void onAttachedToActivity(ActivityPluginBinding binding) {
        activityBinding = binding;
        binding.addRequestPermissionsResultListener(this);
    }

    @Override
    public void onReattachedToActivityForConfigChanges(ActivityPluginBinding binding) {
        onAttachedToActivity(binding);
    }

    @Override
    public void onDetachedFromActivity() {
        Capture capture = current;
        boolean wasRunning = capture != null && capture.startResult == null;
        cancelCurrent();
        if (wasRunning) {
            emit("error", capture.session, "message", "Микрофон отключён при закрытии окна.");
        }
        permissionRequested = false;
        if (activityBinding != null) {
            activityBinding.removeRequestPermissionsResultListener(this);
            activityBinding = null;
        }
    }

    @Override
    public void onDetachedFromActivityForConfigChanges() {
        onDetachedFromActivity();
    }

    @Override
    public void onMethodCall(MethodCall call, MethodChannel.Result result) {
        if (!call.method.equals("start") && !call.method.equals("stop")) {
            result.notImplemented();
            return;
        }
        Integer session = call.argument("session");
        if (session == null) {
            result.error("argument", "Отсутствует идентификатор записи.", null);
            return;
        }
        if (call.method.equals("stop")) {
            if (current != null && current.session == session) {
                cancelCurrent();
            }
            result.success(null);
            return;
        }
        String modelPath = call.argument("model_path");
        if (modelPath == null || activityBinding == null) {
            result.error("unavailable", "Микрофон пока недоступен.", null);
            return;
        }

        cancelCurrent();
        Capture capture = new Capture(session, modelPath, result);
        current = capture;
        Activity activity = activityBinding.getActivity();
        if (activity.checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                == PackageManager.PERMISSION_GRANTED) {
            worker.execute(capture::record);
        } else if (!permissionRequested) {
            try {
                permissionRequested = true;
                activity.requestPermissions(
                        new String[]{Manifest.permission.RECORD_AUDIO}, MICROPHONE_PERMISSION);
            } catch (RuntimeException error) {
                permissionRequested = false;
                fail(capture, "Не удалось запросить доступ к микрофону.");
            }
        }
    }

    @Override
    public boolean onRequestPermissionsResult(int requestCode, String[] permissions,
            int[] grantResults) {
        if (requestCode != MICROPHONE_PERMISSION) {
            return false;
        }
        permissionRequested = false;
        Capture capture = current;
        if (capture != null) {
            if (grantResults.length == 1 && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
                worker.execute(capture::record);
            } else {
                fail(capture, "Доступ к микрофону не разрешён.");
            }
        }
        return true;
    }

    private void cancelCurrent() {
        Capture capture = current;
        current = null;
        if (capture != null) {
            capture.cancel();
            if (capture.startResult != null) {
                capture.startResult.error("cancelled", "Запуск микрофона отменён.", null);
                capture.startResult = null;
            }
        }
    }

    private void fail(Capture capture, String message) {
        if (current != capture) {
            return;
        }
        current = null;
        if (capture.startResult != null) {
            capture.startResult.error("microphone", message, null);
            capture.startResult = null;
        } else {
            emit("error", capture.session, "message", message);
        }
    }

    private void emit(String method, int session, String key, String value) {
        Map<String, Object> data = new HashMap<>();
        data.put("session", session);
        data.put(key, value);
        if (channel != null) {
            channel.invokeMethod(method, data);
        }
    }

    private final class Capture {
        private final int session;
        private final String modelPath;
        private MethodChannel.Result startResult;
        private volatile boolean cancelled;
        private AudioRecord microphone;

        Capture(int session, String modelPath, MethodChannel.Result result) {
            this.session = session;
            this.modelPath = modelPath;
            this.startResult = result;
        }

        @SuppressLint("MissingPermission")
        void record() {
            if (cancelled) {
                return;
            }
            try (Model model = new Model(modelPath);
                    Recognizer recognizer = new Recognizer(model, SAMPLE_RATE)) {
                int encoding = AudioFormat.ENCODING_PCM_16BIT;
                int channelMask = AudioFormat.CHANNEL_IN_MONO;
                int bufferBytes = AudioRecord.getMinBufferSize(SAMPLE_RATE, channelMask, encoding);
                if (bufferBytes <= 0) {
                    throw new IOException("Формат записи не поддерживается микрофоном.");
                }
                AudioRecord input;
                synchronized (this) {
                    if (cancelled) {
                        return;
                    }
                    input = new AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION,
                            SAMPLE_RATE, channelMask, encoding, Math.max(bufferBytes, CHUNK_SAMPLES * 4));
                    microphone = input;
                    if (input.getState() != AudioRecord.STATE_INITIALIZED) {
                        throw new IOException("Не удалось открыть микрофон.");
                    }
                    input.startRecording();
                    if (input.getRecordingState() != AudioRecord.RECORDSTATE_RECORDING) {
                        throw new IOException("Не удалось начать запись с микрофона.");
                    }
                }

                main.post(() -> {
                    if (current == this && startResult != null) {
                        startResult.success(null);
                        startResult = null;
                    }
                });
                short[] audio = new short[CHUNK_SAMPLES];
                long lastAudio = SystemClock.elapsedRealtime();
                long checkSilencedAt = lastAudio;
                while (!cancelled) {
                    int count = input.read(audio, 0, audio.length, AudioRecord.READ_NON_BLOCKING);
                    if (count > 0) {
                        lastAudio = SystemClock.elapsedRealtime();
                        if (Build.VERSION.SDK_INT >= 29 && lastAudio >= checkSilencedAt) {
                            AudioRecordingConfiguration config = input.getActiveRecordingConfiguration();
                            if (config != null && config.isClientSilenced()) {
                                throw new IOException("Android отключил доступ приложения к звуку.");
                            }
                            checkSilencedAt = lastAudio + 500;
                        }
                        if (recognizer.acceptWaveForm(audio, count)) {
                            readPhrase(recognizer.getResult());
                        }
                    } else if (count < 0 || SystemClock.elapsedRealtime() - lastAudio >= 1000) {
                        throw new IOException("Микрофон перестал передавать звук.");
                    } else {
                        SystemClock.sleep(10);
                    }
                }
            } catch (IOException | JSONException | RuntimeException | LinkageError error) {
                String details = error.getMessage();
                main.post(() -> fail(this,
                        details == null ? "Распознавание речи прервано." : details));
            } finally {
                release();
            }
        }

        private void readPhrase(String result) throws JSONException {
            String phrase = new JSONObject(result).optString("text").trim();
            if (!phrase.isEmpty()) {
                long capturedAt = SystemClock.elapsedRealtime();
                main.post(() -> {
                    if (current == this && SystemClock.elapsedRealtime() - capturedAt < 500) {
                        emit("phrase", session, "text", phrase);
                    }
                });
            }
        }

        synchronized void cancel() {
            cancelled = true;
        }

        private synchronized void release() {
            if (microphone != null) {
                try {
                    if (microphone.getRecordingState() == AudioRecord.RECORDSTATE_RECORDING) {
                        microphone.stop();
                    }
                } finally {
                    microphone.release();
                    microphone = null;
                }
            }
        }
    }
}
