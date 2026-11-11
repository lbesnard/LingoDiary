import 'package:flutter/foundation.dart' show kIsWeb;
import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:shared_preferences/shared_preferences.dart';
import '../services/api_service.dart';
import '../services/auth_service.dart';
import '../l10n/app_localizations.dart';

class LoginScreen extends StatefulWidget {
  const LoginScreen({super.key});

  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen> {
  final _formKey = GlobalKey<FormState>();
  final _serverController = TextEditingController(text: 'http://');
  final _usernameController = TextEditingController();
  final _passwordController = TextEditingController();
  bool _loading = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    if (!kIsWeb) _loadSavedServer();
  }

  @override
  void dispose() {
    _serverController.dispose();
    _usernameController.dispose();
    _passwordController.dispose();
    super.dispose();
  }

  Future<void> _loadSavedServer() async {
    final prefs = await SharedPreferences.getInstance();
    final saved = prefs.getString('server_url');
    if (saved != null && mounted) _serverController.text = saved;
  }

  Future<void> _login() async {
    if (_formKey.currentState?.validate() != true) return;
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      // On web the app is served from the same origin — use relative URL ('').
      final serverUrl = kIsWeb ? '' : _serverController.text.trim();
      final result = await ApiService.login(
        serverUrl,
        _usernameController.text.trim(),
        _passwordController.text,
      );
      await AuthService.saveSession(
        result['token'] as String,
        serverUrl,
      );
      await AuthService.saveUsername(_usernameController.text.trim());
      // Fetch and cache TPRS keywords for the TPRS renderer
      try {
        final keywords = await ApiService.fetchTprsKeywords();
        final prefs = await SharedPreferences.getInstance();
        await prefs.setString('tprs_sentence', keywords['sentence']!);
        await prefs.setString('tprs_question', keywords['question']!);
        await prefs.setString('tprs_answer', keywords['answer']!);
      } catch (_) {
        // Non-fatal: renderer will use built-in defaults
      }
      if (mounted) {
        // Replace with main shell
        context.go('/');
      }
    } on ApiException catch (e) {
      setState(() => _error = e.message);
    } catch (e) {
      setState(() => _error =
          AppLocalizations.of(context).loginConnectionError(e.toString()));
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final l10n = AppLocalizations.of(context);
    return Scaffold(
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(24),
            child: Form(
              key: _formKey,
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Image.asset(
                    'assets/icon/app_icon.png',
                    width: 80,
                    height: 80,
                    fit: BoxFit.contain,
                  ),
                  const SizedBox(height: 16),
                  Text('LingoDiary',
                      style: Theme.of(context).textTheme.headlineMedium,
                      textAlign: TextAlign.center),
                  const SizedBox(height: 32),
                  if (!kIsWeb) ...[
                    TextFormField(
                      controller: _serverController,
                      decoration: InputDecoration(
                        labelText: l10n.loginServerUrl,
                        hintText: l10n.loginServerHint,
                        border: OutlineInputBorder(),
                      ),
                      keyboardType: TextInputType.url,
                      validator: (v) => (v == null || v.isEmpty)
                          ? l10n.loginServerRequired
                          : null,
                    ),
                    const SizedBox(height: 12),
                  ],
                  TextFormField(
                    controller: _usernameController,
                    decoration: InputDecoration(
                      labelText: l10n.loginUsername,
                      border: OutlineInputBorder(),
                    ),
                    validator: (v) => (v == null || v.isEmpty)
                        ? l10n.loginUsernameRequired
                        : null,
                  ),
                  const SizedBox(height: 12),
                  TextFormField(
                    controller: _passwordController,
                    decoration: InputDecoration(
                      labelText: l10n.loginPassword,
                      border: OutlineInputBorder(),
                    ),
                    obscureText: true,
                    validator: (v) => (v == null || v.isEmpty)
                        ? l10n.loginPasswordRequired
                        : null,
                  ),
                  if (_error != null) ...[
                    const SizedBox(height: 12),
                    Text(_error!,
                        style: const TextStyle(color: Colors.red),
                        textAlign: TextAlign.center),
                  ],
                  const SizedBox(height: 20),
                  ElevatedButton(
                    onPressed: _loading ? null : _login,
                    child: _loading
                        ? const SizedBox(
                            height: 20,
                            width: 20,
                            child: CircularProgressIndicator(strokeWidth: 2))
                        : const Text('Login'),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}
