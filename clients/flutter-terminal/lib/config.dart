import 'package:shared_preferences/shared_preferences.dart';

/// Persisted app settings (orchestrator endpoint + auth + default target machine).
class AppConfig {
  String baseUrl;
  String secretKey;
  String targetMachine;

  AppConfig({
    required this.baseUrl,
    required this.secretKey,
    required this.targetMachine,
  });

  static const _kUrl = 'orchestrator_url';
  static const _kKey = 'secret_key';
  static const _kMachine = 'target_machine';

  static Future<AppConfig> load() async {
    final p = await SharedPreferences.getInstance();
    return AppConfig(
      // Default = MacBook orchestrator Tailscale IP (queue server on :8000)
      baseUrl: p.getString(_kUrl) ?? 'http://100.97.176.37:8000',
      secretKey: p.getString(_kKey) ?? '',
      targetMachine: p.getString(_kMachine) ?? 'mac-mini',
    );
  }

  Future<void> save() async {
    final p = await SharedPreferences.getInstance();
    await p.setString(_kUrl, baseUrl);
    await p.setString(_kKey, secretKey);
    await p.setString(_kMachine, targetMachine);
  }
}
