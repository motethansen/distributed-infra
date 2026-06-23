import 'package:flutter/material.dart';

import '../config.dart';
import '../orchestrator_api.dart';

/// Link the app to YOUR fleet: orchestrator URL (on your Tailscale account),
/// the shared secret, and which machine should run agents. Persisted on device.
class SettingsScreen extends StatefulWidget {
  final AppConfig config;
  const SettingsScreen({super.key, required this.config});

  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  late final _url = TextEditingController(text: widget.config.baseUrl);
  late final _key = TextEditingController(text: widget.config.secretKey);
  late final _machine = TextEditingController(text: widget.config.targetMachine);

  bool _testing = false;
  String? _status;
  bool _statusOk = false;

  Future<void> _test() async {
    setState(() {
      _testing = true;
      _status = null;
    });
    final api = OrchestratorApi(
      baseUrl: _url.text.trim().replaceAll(RegExp(r'/+$'), ''),
      secretKey: _key.text.trim(),
    );
    final res = await api.testConnection();
    if (!mounted) return;
    setState(() {
      _testing = false;
      _status = res.message;
      _statusOk = res.ok;
    });
  }

  Future<void> _save() async {
    widget.config
      ..baseUrl = _url.text.trim().replaceAll(RegExp(r'/+$'), '')
      ..secretKey = _key.text.trim()
      ..targetMachine = _machine.text.trim();
    await widget.config.save();
    if (mounted) Navigator.pop(context);
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Link your fleet')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          TextField(
            controller: _url,
            keyboardType: TextInputType.url,
            autocorrect: false,
            decoration: const InputDecoration(
              labelText: 'Orchestrator URL',
              helperText: 'Your queue server over Tailscale, e.g. http://100.x.y.z:8000\n'
                  'or http://<host>.<tailnet>.ts.net:8000',
              helperMaxLines: 3,
            ),
          ),
          const SizedBox(height: 16),
          TextField(
            controller: _key,
            obscureText: true,
            autocorrect: false,
            decoration: const InputDecoration(
              labelText: 'Secret key (x-secret-key)',
              helperText: 'The shared SECRET_KEY your fleet uses for queue auth.',
            ),
          ),
          const SizedBox(height: 16),
          TextField(
            controller: _machine,
            autocorrect: false,
            decoration: const InputDecoration(
              labelText: 'Target machine',
              helperText: 'Worker that runs the agents, e.g. mac-mini.',
            ),
          ),
          const SizedBox(height: 24),
          Row(
            children: [
              OutlinedButton.icon(
                onPressed: _testing ? null : _test,
                icon: _testing
                    ? const SizedBox(
                        height: 16, width: 16, child: CircularProgressIndicator(strokeWidth: 2))
                    : const Icon(Icons.wifi_tethering),
                label: const Text('Test connection'),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: FilledButton(onPressed: _save, child: const Text('Save')),
              ),
            ],
          ),
          if (_status != null) ...[
            const SizedBox(height: 16),
            Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Icon(_statusOk ? Icons.check_circle : Icons.error,
                    color: _statusOk ? Colors.greenAccent : Colors.redAccent, size: 20),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(_status!,
                      style: TextStyle(
                          color: _statusOk ? Colors.greenAccent : Colors.redAccent)),
                ),
              ],
            ),
          ],
          const SizedBox(height: 24),
          const Text(
            'Your phone must be on the same Tailscale network as the orchestrator. '
            'Agents (claude/agy/codex) must be logged in on the target machine.',
            style: TextStyle(color: Colors.white54, fontSize: 12),
          ),
        ],
      ),
    );
  }
}
