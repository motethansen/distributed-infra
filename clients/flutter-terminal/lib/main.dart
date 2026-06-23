import 'package:flutter/material.dart';

import 'screens/chat_screen.dart';

void main() => runApp(const AgentTerminalApp());

class AgentTerminalApp extends StatelessWidget {
  const AgentTerminalApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Agent Terminal',
      debugShowCheckedModeBanner: false,
      theme: ThemeData.dark(useMaterial3: true).copyWith(
        scaffoldBackgroundColor: const Color(0xFF0B0F14),
      ),
      home: const ChatScreen(),
    );
  }
}
