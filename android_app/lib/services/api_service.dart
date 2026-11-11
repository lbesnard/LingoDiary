import 'dart:convert';
import 'package:flutter/foundation.dart' show kIsWeb;
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';
import 'auth_service.dart';

class ApiException implements Exception {
  final int statusCode;
  final String message;
  ApiException(this.statusCode, this.message);

  @override
  String toString() => 'ApiException($statusCode): $message';
}

class ApiService {
  static const _timeout = Duration(seconds: 15);

  /// Returns the API base URL.
  /// On web the app is served from the same origin, so relative URLs are used.
  static Future<String> baseUrl() async {
    if (kIsWeb) return '';
    final prefs = await SharedPreferences.getInstance();
    final url = prefs.getString('server_url') ?? '';
    return url.endsWith('/') ? url.substring(0, url.length - 1) : url;
  }

  // Keep a private alias so existing internal callers are unchanged.
  static Future<String> _baseUrl() => baseUrl();

  /// Percent-encodes each path segment (unlike [Uri.encodeFull], this also
  /// escapes `?` and `#`, which can legitimately appear in lesson titles).
  static String encodePath(String relPath) =>
      relPath.split('/').map(Uri.encodeComponent).join('/');

  static String _errorMessage(http.Response resp, String fallback) {
    try {
      final body = jsonDecode(resp.body);
      if (body is Map && body['error'] is String) return body['error'] as String;
    } catch (_) {
      // Non-JSON error body (proxy page, etc.) — use the fallback.
    }
    return fallback;
  }

  /// Throws [ApiException] unless the response is 200.  A 401 also clears the
  /// stored session so the app falls back to the login screen.
  static Future<void> _expectOk(http.Response resp, String fallback) async {
    if (resp.statusCode == 200) return;
    if (resp.statusCode == 401) {
      await AuthService.clearSession();
      throw ApiException(401, 'Session expired. Please log in again.');
    }
    throw ApiException(resp.statusCode, _errorMessage(resp, fallback));
  }

  static Future<Map<String, String>> _authHeaders() async {
    final token = await AuthService.getToken();
    return {
      'Content-Type': 'application/json',
      if (token != null) 'Authorization': 'Bearer $token',
    };
  }

  static Future<Map<String, dynamic>> login(
      String serverUrl, String username, String password) async {
    final url = serverUrl.endsWith('/') ? serverUrl.substring(0, serverUrl.length - 1) : serverUrl;
    final resp = await http
        .post(
          Uri.parse('$url/api/login'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode({'username': username, 'password': password}),
        )
        .timeout(_timeout);
    if (resp.statusCode != 200) {
      throw ApiException(resp.statusCode, _errorMessage(resp, 'Login failed'));
    }
    return jsonDecode(resp.body) as Map<String, dynamic>;
  }

  static Future<void> triggerGenerate() async {
    final base = await _baseUrl();
    final headers = await _authHeaders();
    final resp =
        await http.post(Uri.parse('$base/api/generate'), headers: headers).timeout(_timeout);
    await _expectOk(resp, 'Failed to trigger generation');
  }

  static Future<Map<String, dynamic>> getGenerateStatus({int offset = 0}) async {
    final base = await _baseUrl();
    final headers = await _authHeaders();
    final uri = Uri.parse('$base/api/generate/status').replace(
      queryParameters: {'offset': '$offset'},
    );
    final resp = await http.get(uri, headers: headers).timeout(_timeout);
    if (resp.statusCode != 200) return {'log': '', 'offset': offset, 'done': false};
    final data = jsonDecode(resp.body) as Map<String, dynamic>;
    return {
      'log': data['log'] as String? ?? '',
      'offset': data['offset'] as int? ?? offset,
      'done': data['done'] as bool? ?? false,
    };
  }

  static Future<List<Map<String, dynamic>>> getLessons() async {
    final base = await _baseUrl();
    final headers = await _authHeaders();
    final resp =
        await http.get(Uri.parse('$base/api/lessons'), headers: headers).timeout(_timeout);
    await _expectOk(resp, 'Failed to get lessons');
    final data = jsonDecode(resp.body) as Map<String, dynamic>;
    return List<Map<String, dynamic>>.from(data['lessons'] as List);
  }

  static Future<List<Map<String, dynamic>>> getSyncManifest() async {
    final base = await _baseUrl();
    final headers = await _authHeaders();
    final resp = await http
        .get(Uri.parse('$base/api/sync/manifest'), headers: headers)
        .timeout(_timeout);
    await _expectOk(resp, 'Failed to get manifest');
    final data = jsonDecode(resp.body) as Map<String, dynamic>;
    return List<Map<String, dynamic>>.from(data['manifest'] as List);
  }

  static Future<http.Response> downloadFile(String relPath) async {
    final base = await _baseUrl();
    final headers = await _authHeaders();
    final encoded = '$base/api/sync/file/${encodePath(relPath)}';
    return http.get(Uri.parse(encoded), headers: headers).timeout(const Duration(minutes: 2));
  }

  static Future<List<Map<String, dynamic>>> getLessonManifest(String lessonBase) async {
    final serverBase = await _baseUrl();
    final headers = await _authHeaders();
    // Use pathSegments to properly percent-encode spaces and emoji in lessonBase
    final serverUri = Uri.parse(serverBase);
    final uri = Uri(
      scheme: serverUri.scheme,
      host: serverUri.host,
      port: serverUri.port,
      pathSegments: [...serverUri.pathSegments, 'api', 'sync', 'manifest', lessonBase],
    );
    final resp = await http.get(uri, headers: headers).timeout(_timeout);
    await _expectOk(resp, 'Failed to get lesson manifest');
    final data = jsonDecode(resp.body) as Map<String, dynamic>;
    return List<Map<String, dynamic>>.from(data['manifest'] as List);
  }

  static Future<void> addSentences(String date, List<String> sentences) async {
    final base = await _baseUrl();
    final headers = await _authHeaders();
    final resp = await http
        .post(
          Uri.parse('$base/api/diary/entry'),
          headers: headers,
          body: jsonEncode({'date': date, 'sentences': sentences}),
        )
        .timeout(_timeout);
    await _expectOk(resp, 'Failed to add diary entry');
  }

  static Future<Map<String, String>> fetchTprsKeywords() async {
    final base = await _baseUrl();
    final headers = await _authHeaders();
    final resp =
        await http.get(Uri.parse('$base/api/config'), headers: headers).timeout(_timeout);
    await _expectOk(resp, 'Failed to get config');
    final data = (jsonDecode(resp.body) as Map<String, dynamic>)['tprs'] as Map<String, dynamic>;
    return {
      'sentence': data['sentence'] as String? ?? 'SETNING:',
      'question': data['question'] as String? ?? 'SPØRSMÅL:',
      'answer': data['answer'] as String? ?? 'SVAR:',
    };
  }

  /// Fetch app config including Drive Mode availability.
  static Future<Map<String, dynamic>> fetchAppConfig() async {
    final base = await _baseUrl();
    final headers = await _authHeaders();
    final resp =
        await http.get(Uri.parse('$base/api/config'), headers: headers).timeout(_timeout);
    await _expectOk(resp, 'Failed to get config');
    return jsonDecode(resp.body) as Map<String, dynamic>;
  }

  /// Trigger Drive Mode audio backfill on the server.
  /// [date] is optional YYYY-MM-DD; if null, all days are processed.
  static Future<void> triggerDriveModeAudioBackfill({String? date}) async {
    final base = await _baseUrl();
    final headers = await _authHeaders();
    final path = date != null
        ? '$base/api/backfill/drive_mode_audio/${date.replaceAll('/', '-')}'
        : '$base/api/backfill/drive_mode_audio';
    final resp = await http.post(Uri.parse(path), headers: headers).timeout(_timeout);
    await _expectOk(resp, 'Failed to start Drive Mode audio backfill');
  }

  /// Poll the generate status endpoint for Drive Mode generation progress.
  static Future<Map<String, dynamic>> pollGenerateStatus(int offset) async {
    final base = await _baseUrl();
    final headers = await _authHeaders();
    final resp = await http
        .get(Uri.parse('$base/api/generate/status?offset=$offset'), headers: headers)
        .timeout(_timeout);
    await _expectOk(resp, 'Status check failed');
    return jsonDecode(resp.body) as Map<String, dynamic>;
  }

  // ── Diary JSON / SRS endpoints ──────────────────────────────────────────────

  /// Fetch all lesson entries (with audio timings) for a date + variant.
  /// [date] in YYYY-MM-DD or YYYY/MM/DD format.
  static Future<Map<String, dynamic>> getLessonEntries(
      String date, String variant) async {
    final base = await _baseUrl();
    final headers = await _authHeaders();
    // Normalise to YYYY/MM/DD for the URL
    final dateFmt = date.replaceAll('/', '-');
    final resp = await http
        .get(
          Uri.parse('$base/api/lessons/entries/$dateFmt/$variant'),
          headers: headers,
        )
        .timeout(_timeout);
    await _expectOk(resp, 'Failed to get lesson entries');
    return jsonDecode(resp.body) as Map<String, dynamic>;
  }

  /// Fetch the complete Day object for a given date (all variants, all entries, title, etc.).
  /// [date] in YYYY-MM-DD or YYYY/MM/DD format.
  static Future<Map<String, dynamic>> getDayData(String date) async {
    final base = await _baseUrl();
    final headers = await _authHeaders();
    final dateFmt = date.replaceAll('/', '-');
    final resp = await http
        .get(
          Uri.parse('$base/api/diary/day/$dateFmt'),
          headers: headers,
        )
        .timeout(_timeout);
    await _expectOk(resp, 'Failed to get day data');
    return jsonDecode(resp.body) as Map<String, dynamic>;
  }

  /// Score an individual entry via SM-2.
  /// [score]: 0=Again, 2=Hard, 3=Good, 5=Easy
  /// [scoredAt] is when the review really happened (used when replaying
  /// offline reviews so spaced-repetition intervals start from that moment).
  static Future<Map<String, dynamic>> scoreEntry(
      String date, int entryIndex, int score,
      {DateTime? scoredAt}) async {
    final base = await _baseUrl();
    final headers = await _authHeaders();
    final resp = await http
        .post(
          Uri.parse('$base/api/lessons/score'),
          headers: headers,
          body: jsonEncode({
            'date': date,
            'entry_index': entryIndex,
            'score': score,
            if (scoredAt != null) 'scored_at': scoredAt.toUtc().toIso8601String(),
          }),
        )
        .timeout(_timeout);
    await _expectOk(resp, 'Failed to score entry');
    return jsonDecode(resp.body) as Map<String, dynamic>;
  }

  /// Fetch home dashboard data: stats, recent lessons, recommendation.
  static Future<Map<String, dynamic>> getHomeData() async {
    final base = await _baseUrl();
    final headers = await _authHeaders();
    final resp = await http
        .get(Uri.parse('$base/api/home'), headers: headers)
        .timeout(_timeout);
    await _expectOk(resp, 'Failed to get home data');
    return jsonDecode(resp.body) as Map<String, dynamic>;
  }

  /// Fetch sentences due for review (Anki-style).
  static Future<List<Map<String, dynamic>>> getDueSentences({
    int limit = 20,
    String variant = 'original',
  }) async {
    final base = await _baseUrl();
    final headers = await _authHeaders();
    final uri = Uri.parse('$base/api/sentences/due').replace(
      queryParameters: {'limit': '$limit', 'variant': variant},
    );
    final resp = await http.get(uri, headers: headers).timeout(_timeout);
    await _expectOk(resp, 'Failed to get due sentences');
    final data = jsonDecode(resp.body) as Map<String, dynamic>;
    return List<Map<String, dynamic>>.from(data['sentences'] as List);
  }

  /// Trigger Q&A translation backfill (background job on server).
  static Future<void> triggerQaTranslationBackfill() async {
    final base = await _baseUrl();
    final headers = await _authHeaders();
    final resp = await http
        .post(Uri.parse('$base/api/backfill/qa_translations'), headers: headers)
        .timeout(_timeout);
    await _expectOk(resp, 'Failed to start Q&A translation backfill');
  }

  /// Trigger audio timing backfill (background job on server).
  /// Regenerates per-sentence segment MP3s and writes timing data to diary.json.
  /// When [overwrite] is true, forces TTS re-generation for all days (slow).
  /// When false (default), only recomputes timing from existing segment files
  /// when zero timing is detected — much faster.
  static Future<void> triggerAudioTimingBackfill({bool overwrite = false}) async {
    final base = await _baseUrl();
    final headers = await _authHeaders();
    final resp = await http
        .post(
          Uri.parse('$base/api/backfill/audio_timing'),
          headers: headers,
          body: jsonEncode({'overwrite': overwrite}),
        )
        .timeout(_timeout);
    await _expectOk(resp, 'Failed to start audio timing backfill');
  }

  /// Trigger full maintenance backfill (background job on server).
  /// Runs in sequence: sync diary.json → Q&A translations → audio timing.
  static Future<void> triggerBackfillAll() async {
    final base = await _baseUrl();
    final headers = await _authHeaders();
    final resp = await http
        .post(Uri.parse('$base/api/backfill/all'), headers: headers)
        .timeout(_timeout);
    await _expectOk(resp, 'Failed to start full backfill');
  }

  /// Get the last_reviewed timestamp for a specific lesson date
  static Future<Map<String, dynamic>> getLessonLastReviewed(String date) async {
    final base = await _baseUrl();
    final headers = await _authHeaders();
    final dateFmt = date.replaceAll('/', '-');
    final resp = await http
        .get(
          Uri.parse('$base/api/lessons/last_reviewed/$dateFmt'),
          headers: headers,
        )
        .timeout(_timeout);
    await _expectOk(resp, 'Failed to get lesson last_reviewed');
    return jsonDecode(resp.body) as Map<String, dynamic>;
  }

  /// Update the last_reviewed timestamp for a specific lesson date
  static Future<Map<String, dynamic>> updateLessonLastReviewed(String date, {String? timestamp}) async {
    final base = await _baseUrl();
    final headers = await _authHeaders();
    final dateFmt = date.replaceAll('/', '-');
    final body = timestamp != null ? {'timestamp': timestamp} : {};

    final resp = await http
        .put(
          Uri.parse('$base/api/lessons/last_reviewed/$dateFmt'),
          headers: headers,
          body: jsonEncode(body),
        )
        .timeout(_timeout);
    await _expectOk(resp, 'Failed to update lesson last_reviewed');
    return jsonDecode(resp.body) as Map<String, dynamic>;
  }

  /// Get recently studied lessons (by last_reviewed timestamp)
  static Future<List<Map<String, dynamic>>> getRecentlyStudiedLessons({int limit = 10}) async {
    final base = await _baseUrl();
    final headers = await _authHeaders();
    final uri = Uri.parse('$base/api/lessons/recently_studied').replace(
      queryParameters: {'limit': '$limit'},
    );
    final resp = await http.get(uri, headers: headers).timeout(_timeout);
    await _expectOk(resp, 'Failed to get recently studied lessons');
    final data = jsonDecode(resp.body) as Map<String, dynamic>;
    return List<Map<String, dynamic>>.from(data['lessons'] as List);
  }

  static Future<void> saveTrials(String date, List<String> trials) async {
    final base = await _baseUrl();
    final headers = await _authHeaders();
    final resp = await http
        .post(
          Uri.parse('$base/api/diary/${Uri.encodeComponent(date)}/trials'),
          headers: headers,
          body: jsonEncode({'trials': trials}),
        )
        .timeout(_timeout);
    await _expectOk(resp, 'Failed to save trials');
  }
}
