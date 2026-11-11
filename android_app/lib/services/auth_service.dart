import 'package:flutter/foundation.dart' show kIsWeb;
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:shared_preferences/shared_preferences.dart';

class AuthService {
  static const _storage = FlutterSecureStorage();
  static const _tokenKey = 'jwt_token';
  static const _serverUrlKey = 'server_url';
  static const _usernameKey = 'username';

  static Future<void> saveSession(String token, String serverUrl) async {
    // On web, FlutterSecureStorage can throw (no OS keychain).
    // SharedPreferences (localStorage) is equivalent security on web.
    if (kIsWeb) {
      final prefs = await SharedPreferences.getInstance();
      await prefs.setString(_tokenKey, token);
      await prefs.setString(_serverUrlKey, serverUrl);
    } else {
      await _storage.write(key: _tokenKey, value: token);
      final prefs = await SharedPreferences.getInstance();
      await prefs.setString(_serverUrlKey, serverUrl);
    }
  }

  static Future<void> saveUsername(String username) async {
    if (kIsWeb) {
      final prefs = await SharedPreferences.getInstance();
      await prefs.setString(_usernameKey, username);
    } else {
      await _storage.write(key: _usernameKey, value: username);
      // Also store in SharedPreferences as fallback (SecureStorage can be
      // unavailable on first boot or after re-install)
      final prefs = await SharedPreferences.getInstance();
      await prefs.setString(_usernameKey, username);
    }
  }

  static Future<String?> getUsername() async {
    if (kIsWeb) {
      final prefs = await SharedPreferences.getInstance();
      return prefs.getString(_usernameKey);
    }
    try {
      final fromStorage = await _storage.read(key: _usernameKey);
      if (fromStorage != null && fromStorage.isNotEmpty) return fromStorage;
    } catch (_) {}
    // Fallback to SharedPreferences
    final prefs = await SharedPreferences.getInstance();
    return prefs.getString(_usernameKey);
  }

  static Future<String?> getToken() async {
    if (kIsWeb) {
      final prefs = await SharedPreferences.getInstance();
      return prefs.getString(_tokenKey);
    }
    return _storage.read(key: _tokenKey);
  }

  /// Clears JWT token (and username) but keeps server_url and cached data.
  static Future<void> clearSession() async {
    if (kIsWeb) {
      final prefs = await SharedPreferences.getInstance();
      await prefs.remove(_tokenKey);
      await prefs.remove(_usernameKey);
      try {
        await prefs.reload();
      } catch (_) {}
    } else {
      await _storage.delete(key: _tokenKey);
      await _storage.delete(key: _usernameKey);
      final prefs = await SharedPreferences.getInstance();
      await prefs.remove(_usernameKey);
    }
  }
}
