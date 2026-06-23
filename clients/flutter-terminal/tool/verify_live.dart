// Live end-to-end check of the app's OrchestratorApi against the real orchestrator.
// Runs the SAME networking code the UI uses (lib/orchestrator_api.dart).
//
//   ORCHESTRATOR_URL=http://100.97.176.37:8000 INFRA_SECRET_KEY=… \
//     dart run tool/verify_live.dart
import 'dart:io';
import 'dart:math';

import 'package:agent_terminal/orchestrator_api.dart';

String uuidV4() {
  final r = Random.secure();
  final b = List<int>.generate(16, (_) => r.nextInt(256));
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  String h(int i) => b[i].toRadixString(16).padLeft(2, '0');
  return '${h(0)}${h(1)}${h(2)}${h(3)}-${h(4)}${h(5)}-${h(6)}${h(7)}-'
      '${h(8)}${h(9)}-${h(10)}${h(11)}${h(12)}${h(13)}${h(14)}${h(15)}';
}

Future<void> main() async {
  final base = Platform.environment['ORCHESTRATOR_URL'] ?? 'http://100.97.176.37:8000';
  final key = Platform.environment['INFRA_SECRET_KEY'] ?? '';
  if (key.isEmpty) {
    stderr.writeln('set INFRA_SECRET_KEY');
    exit(1);
  }
  final api = OrchestratorApi(baseUrl: base, secretKey: key);
  stdout.writeln('orchestrator: $base');

  stdout.writeln('\n== machines() ==');
  final m = await api.machines();
  stdout.writeln('machines returned: ${m.length}');

  stdout.writeln('\n== agy (one-shot) ==');
  final id0 = await api.submitAgentTask(
      agent: 'agy',
      prompt: 'Reply with exactly one short sentence confirming you are working.',
      targetMachine: 'mac-mini');
  final r0 = await api.awaitResult(id0);
  stdout.writeln('[${r0.status}] ${r0.output}');

  stdout.writeln('\n== claude (multi-turn) ==');
  final sid = uuidV4();
  stdout.writeln('session_id: $sid');
  final id1 = await api.submitAgentTask(
      agent: 'claude',
      prompt: 'Remember this codeword: PURPLE-DRAGON-42. Reply with only: OK',
      targetMachine: 'mac-mini',
      sessionId: sid,
      resume: false);
  final r1 = await api.awaitResult(id1);
  stdout.writeln('turn1 [${r1.status}]: ${r1.output}');

  final id2 = await api.submitAgentTask(
      agent: 'claude',
      prompt: 'What codeword did I tell you? Reply with just the codeword.',
      targetMachine: 'mac-mini',
      sessionId: sid,
      resume: true);
  final r2 = await api.awaitResult(id2);
  stdout.writeln('turn2 [${r2.status}]: ${r2.output}');

  final pass = r2.output.contains('PURPLE-DRAGON-42');
  stdout.writeln('\nMULTI-TURN: ${pass ? "PASS ✅ context retained" : "FAIL ❌"}');
  exit(pass ? 0 : 2);
}
