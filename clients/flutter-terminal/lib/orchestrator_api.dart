import 'dart:convert';

import 'package:http/http.dart' as http;

import 'models.dart';

/// Thin client over the distributed-infra orchestrator queue API.
/// Mirrors what the WhatsApp bridge does: enqueue an agent_run task, then poll.
class OrchestratorApi {
  final String baseUrl;
  final String secretKey;

  OrchestratorApi({required this.baseUrl, required this.secretKey});

  Map<String, String> get _headers => {
        'Content-Type': 'application/json',
        'x-secret-key': secretKey,
      };

  /// Enqueue an agent_run task. Returns the task id.
  /// Send BACKEND agent names (claude/agy/codex/groq/content/social) — the
  /// WhatsApp `code`/`gpt` aliases are not understood by the worker runner.
  Future<String> submitAgentTask({
    required String agent,
    required String prompt,
    required String targetMachine,
    String? sessionId,
    bool resume = false,
  }) async {
    final payload = <String, dynamic>{
      'agent': agent,
      'prompt': prompt,
      '_target_machine': targetMachine,
    };
    if (sessionId != null) {
      payload['session_id'] = sessionId;
      payload['resume'] = resume;
    }
    final r = await http.post(
      Uri.parse('$baseUrl/tasks'),
      headers: _headers,
      body: jsonEncode({'type': 'agent_run', 'payload': payload, 'notes': prompt}),
    );
    if (r.statusCode != 201) {
      throw Exception('submit failed: HTTP ${r.statusCode} ${r.body}');
    }
    return (jsonDecode(r.body) as Map<String, dynamic>)['id'] as String;
  }

  Future<AgentTask> getTask(String id) async {
    final r = await http.get(Uri.parse('$baseUrl/tasks/$id'), headers: _headers);
    if (r.statusCode != 200) {
      throw Exception('get failed: HTTP ${r.statusCode}');
    }
    return AgentTask.fromJson(jsonDecode(r.body) as Map<String, dynamic>);
  }

  /// Poll until the task is terminal (or the client times out).
  Future<AgentTask> awaitResult(
    String id, {
    Duration interval = const Duration(seconds: 2),
    Duration timeout = const Duration(minutes: 8),
  }) async {
    final deadline = DateTime.now().add(timeout);
    while (true) {
      final t = await getTask(id);
      if (t.isTerminal) return t;
      if (DateTime.now().isAfter(deadline)) {
        return AgentTask(
            id: id, status: 'failed', result: {'error': 'client timed out waiting'});
      }
      await Future.delayed(interval);
    }
  }

  /// Fleet roster (for a future status view).
  Future<List<dynamic>> machines() async {
    final r = await http.get(Uri.parse('$baseUrl/machines'), headers: _headers);
    if (r.statusCode != 200) return const [];
    return jsonDecode(r.body) as List<dynamic>;
  }
}
