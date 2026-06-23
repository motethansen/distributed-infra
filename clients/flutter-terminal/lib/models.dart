/// Minimal mirror of the orchestrator's Task shape (the fields the app needs).
class AgentTask {
  final String id;
  final String status; // pending | claimed | in_progress | done | failed | needs_human
  final Map<String, dynamic>? result;
  final String? notes;

  AgentTask({required this.id, required this.status, this.result, this.notes});

  factory AgentTask.fromJson(Map<String, dynamic> j) => AgentTask(
        id: j['id'] as String,
        status: (j['status'] as String?) ?? 'pending',
        result: (j['result'] as Map?)?.cast<String, dynamic>(),
        notes: j['notes'] as String?,
      );

  bool get isTerminal =>
      status == 'done' || status == 'failed' || status == 'needs_human';

  /// Best human-readable output for a finished task.
  String get output {
    final r = result ?? const {};
    final v = r['response'] ?? r['error'] ?? notes ?? '';
    return v.toString();
  }
}
