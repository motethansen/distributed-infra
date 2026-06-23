import 'package:flutter/material.dart';

import '../config.dart';

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

  Future<void> _save() async {
    widget.config
      ..baseUrl = _url.text.trim()
      ..secretKey = _key.text.trim()
      ..targetMachine = _machine.text.trim();
    await widget.config.save();
    if (mounted) Navigator.pop(context);
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Settings')),
      body: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            TextField(
              controller: _url,
              decoration: const InputDecoration(
                labelText: 'Orchestrator URL (Tailscale)',
                hintText: 'http://100.97.176.37:8000',
              ),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _key,
              obscureText: true,
              decoration: const InputDecoration(labelText: 'Secret key (x-secret-key)'),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _machine,
              decoration: const InputDecoration(labelText: 'Target machine'),
            ),
            const SizedBox(height: 24),
            FilledButton(onPressed: _save, child: const Text('Save')),
            const SizedBox(height: 12),
            const Text(
              'The phone must be on the Tailscale network to reach the orchestrator.',
              style: TextStyle(color: Colors.white54, fontSize: 12),
            ),
          ],
        ),
      ),
    );
  }
}
