import 'dart:math';

import 'package:flutter/material.dart';

import '../config.dart';
import '../orchestrator_api.dart';
import 'settings_screen.dart';

// Backend agent names (worker agents/runner.py). NOT the WhatsApp aliases.
const _agents = ['claude', 'agy', 'codex', 'groq', 'content', 'social'];
const _resumable = {'claude'};

class _Msg {
  final bool fromUser;
  String text;
  bool running;
  _Msg(this.text, {this.fromUser = false, this.running = false});
}

class ChatScreen extends StatefulWidget {
  const ChatScreen({super.key});

  @override
  State<ChatScreen> createState() => _ChatScreenState();
}

class _ChatScreenState extends State<ChatScreen> {
  AppConfig? _cfg;
  String _agent = 'claude';
  String? _sessionId;
  bool _busy = false;

  final _input = TextEditingController();
  final _scroll = ScrollController();
  final List<_Msg> _msgs = [];

  @override
  void initState() {
    super.initState();
    AppConfig.load().then((c) {
      if (mounted) setState(() => _cfg = c);
    });
  }

  bool get _resumableAgent => _resumable.contains(_agent);

  // RFC-4122 v4 UUID without an extra package.
  String _uuid() {
    final r = Random.secure();
    final b = List<int>.generate(16, (_) => r.nextInt(256));
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    String h(int i) => b[i].toRadixString(16).padLeft(2, '0');
    return '${h(0)}${h(1)}${h(2)}${h(3)}-${h(4)}${h(5)}-${h(6)}${h(7)}-'
        '${h(8)}${h(9)}-${h(10)}${h(11)}${h(12)}${h(13)}${h(14)}${h(15)}';
  }

  Future<void> _send() async {
    final cfg = _cfg;
    final text = _input.text.trim();
    if (cfg == null || text.isEmpty || _busy) return;
    if (cfg.secretKey.isEmpty) {
      _openSettings();
      return;
    }

    final api = OrchestratorApi(baseUrl: cfg.baseUrl, secretKey: cfg.secretKey);
    final resume = _resumableAgent && _sessionId != null;
    if (_resumableAgent && _sessionId == null) _sessionId = _uuid();

    final placeholder = _Msg('…', running: true);
    setState(() {
      _msgs.add(_Msg(text, fromUser: true));
      _msgs.add(placeholder);
      _busy = true;
      _input.clear();
    });
    _autoScroll();

    try {
      final id = await api.submitAgentTask(
        agent: _agent,
        prompt: text,
        targetMachine: cfg.targetMachine,
        sessionId: _resumableAgent ? _sessionId : null,
        resume: resume,
      );
      final res = await api.awaitResult(id);
      var out = res.output.isEmpty ? '(no output)' : res.output;
      if (res.status != 'done') out = '⚠️ ${res.status}: $out';
      placeholder.text = out;
    } catch (e) {
      placeholder.text = '❌ $e';
    } finally {
      placeholder.running = false;
      if (mounted) setState(() => _busy = false);
      _autoScroll();
    }
  }

  void _autoScroll() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scroll.hasClients) {
        _scroll.animateTo(_scroll.position.maxScrollExtent,
            duration: const Duration(milliseconds: 200), curve: Curves.easeOut);
      }
    });
  }

  void _newSession() {
    setState(() {
      _sessionId = null;
      _msgs.add(_Msg('— new session —'));
    });
  }

  Future<void> _openSettings() async {
    final cfg = _cfg;
    if (cfg == null) return;
    await Navigator.push(
        context, MaterialPageRoute(builder: (_) => SettingsScreen(config: cfg)));
    if (mounted) setState(() {});
  }

  @override
  Widget build(BuildContext context) {
    final live = _resumableAgent && _sessionId != null;
    return Scaffold(
      appBar: AppBar(
        title: Text('Agent · $_agent${live ? ' ●' : ''}'),
        actions: [
          IconButton(
              onPressed: _newSession,
              icon: const Icon(Icons.refresh),
              tooltip: 'New session'),
          IconButton(onPressed: _openSettings, icon: const Icon(Icons.settings)),
        ],
      ),
      body: Column(
        children: [
          Expanded(
            child: ListView.builder(
              controller: _scroll,
              padding: const EdgeInsets.all(12),
              itemCount: _msgs.length,
              itemBuilder: (_, i) => _bubble(_msgs[i]),
            ),
          ),
          _composer(),
        ],
      ),
    );
  }

  Widget _bubble(_Msg m) {
    const mono = TextStyle(fontFamily: 'monospace', fontSize: 13, height: 1.35);
    return Align(
      alignment: m.fromUser ? Alignment.centerRight : Alignment.centerLeft,
      child: Container(
        margin: const EdgeInsets.symmetric(vertical: 4),
        padding: const EdgeInsets.all(10),
        constraints: const BoxConstraints(maxWidth: 640),
        decoration: BoxDecoration(
          color: m.fromUser ? const Color(0xFF1E3A5F) : const Color(0xFF11161D),
          borderRadius: BorderRadius.circular(10),
          border: Border.all(color: const Color(0xFF223040)),
        ),
        child: m.running
            ? const SizedBox(
                height: 16,
                width: 16,
                child: CircularProgressIndicator(strokeWidth: 2))
            : SelectableText(m.text, style: mono),
      ),
    );
  }

  Widget _composer() {
    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.all(8),
        child: Row(
          children: [
            DropdownButton<String>(
              value: _agent,
              dropdownColor: const Color(0xFF11161D),
              items: _agents
                  .map((a) => DropdownMenuItem(value: a, child: Text(a)))
                  .toList(),
              onChanged: (v) => setState(() {
                _agent = v!;
                _sessionId = null; // switching agent starts a fresh conversation
              }),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: TextField(
                controller: _input,
                minLines: 1,
                maxLines: 5,
                textInputAction: TextInputAction.send,
                onSubmitted: (_) => _send(),
                decoration: const InputDecoration(
                  hintText: 'Message the agent…',
                  border: OutlineInputBorder(),
                  isDense: true,
                ),
              ),
            ),
            IconButton(
                onPressed: _busy ? null : _send, icon: const Icon(Icons.send)),
          ],
        ),
      ),
    );
  }
}
