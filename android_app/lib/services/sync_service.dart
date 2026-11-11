import 'dart:io';
import 'package:flutter/foundation.dart' show kIsWeb;
import 'package:path_provider/path_provider.dart';
import 'api_service.dart';
import 'auth_service.dart';

class SyncService {
  /// Returns the local directory used to cache synced output files.
  /// Throws UnsupportedError on web (local file system unavailable).
  static Future<Directory> _localOutputDir() async {
    assert(!kIsWeb, '_localOutputDir must not be called on web');
    final base = await getApplicationDocumentsDirectory();
    final dir = Directory('${base.path}/output');
    await dir.create(recursive: true);
    return dir;
  }

  /// Returns the full local path for a given relative output file path.
  /// On web, returns the server URL for that file instead.
  static Future<String> localPath(String relPath) async {
    if (kIsWeb) {
      final base = await ApiService.baseUrl();
      return '$base/api/sync/file/${ApiService.encodePath(relPath)}';
    }
    final dir = await _localOutputDir();
    return '${dir.path}/$relPath';
  }

  /// Returns a [Uri] suitable for use with just_audio on both platforms.
  /// On Android: a file:// URI pointing to the locally cached file.
  /// On web:     an HTTPS URI pointing to the server's sync endpoint,
  ///             with the JWT token as a query param (HTML5 audio can't send headers).
  static Future<Uri> audioUri(String relPath) async {
    if (kIsWeb) {
      final base = await ApiService.baseUrl();
      final token = await AuthService.getToken() ?? '';
      final encoded = ApiService.encodePath(relPath);
      return Uri.parse('$base/api/sync/file/$encoded?token=${Uri.encodeQueryComponent(token)}');
    }
    final path = await localPath(relPath);
    return Uri.file(path);
  }

  /// Downloads a single file from the server and caches it locally.
  /// On web, the file is always streamed from the server so this is a no-op.
  /// Returns true on success, false on any failure (non-200 status or network
  /// error such as SocketException or TimeoutException when offline).
  static Future<bool> downloadFile(String relPath) async {
    if (kIsWeb) return true; // web streams directly from server
    final dir = await _localOutputDir();
    final localFile = File('${dir.path}/$relPath');
    try {
      final resp = await ApiService.downloadFile(relPath);
      if (resp.statusCode == 200) {
        await localFile.parent.create(recursive: true);
        await localFile.writeAsBytes(resp.bodyBytes);
        return true;
      }
    } catch (_) {}
    return false;
  }

  /// Ensures [relPath] is available locally (downloads if missing or stale on
  /// Android), then returns a [Uri] ready for [AudioSource.uri].
  ///
  /// When [forceRefresh] is true and the download fails (e.g. offline), falls
  /// back to the locally cached file if it exists.  Returns null only when no
  /// local copy is available at all.
  /// On web, always returns the authenticated server URI without any local check.
  static Future<Uri?> ensureLocalAndGetUri(String relPath, {bool forceRefresh = false}) async {
    if (relPath.isEmpty) return null;
    if (!kIsWeb) {
      final path = await localPath(relPath);
      if (forceRefresh || !File(path).existsSync()) {
        final ok = await downloadFile(relPath);
        if (!ok) {
          // Offline fallback: serve the locally cached file if it already exists.
          if (!File(path).existsSync()) return null;
        }
      }
    }
    return audioUri(relPath);
  }

  /// Downloads files in [manifest] that are missing or older than the server
  /// copy.  Updates [onProgress]/[onProgressCount] as it goes.
  /// Returns the number of newly downloaded files.
  static Future<int> _syncManifest(
    List<Map<String, dynamic>> manifest, {
    void Function(String message)? onProgress,
    void Function(int current, int total)? onProgressCount,
    bool Function()? isCancelled,
  }) async {
    final dir = await _localOutputDir();
    final total = manifest.length;
    int checked = 0;
    int downloaded = 0;

    for (final entry in manifest) {
      if (isCancelled != null && isCancelled()) break;
      checked++;
      final relPath = entry['path'] as String;
      final serverMtime = (entry['mtime'] as num).toDouble();
      final localFile = File('${dir.path}/$relPath');

      bool needsDownload = true;
      if (await localFile.exists()) {
        final localMtime = (await localFile.lastModified()).millisecondsSinceEpoch / 1000.0;
        if (localMtime >= serverMtime) needsDownload = false;
      }

      if (needsDownload) {
        onProgress?.call('Downloading $relPath …');
        final resp = await ApiService.downloadFile(relPath);
        if (resp.statusCode == 200) {
          await localFile.parent.create(recursive: true);
          // Write to a temp file first so an interrupted download never
          // leaves a truncated mp3 that looks "up to date" by mtime.
          final tmp = File('${localFile.path}.part');
          await tmp.writeAsBytes(resp.bodyBytes, flush: true);
          await tmp.rename(localFile.path);
          downloaded++;
        }
      }
      onProgressCount?.call(checked, total);
    }
    return downloaded;
  }

  /// Syncs all output files from the server.
  /// On web this is a no-op (audio is always streamed from the server).
  static Future<int> syncFromServer({
    void Function(String message)? onProgress,
    void Function(int current, int total)? onProgressCount,
    bool Function()? isCancelled,
  }) async {
    if (kIsWeb) return 0;
    final manifest = await ApiService.getSyncManifest();
    return _syncManifest(manifest,
        onProgress: onProgress,
        onProgressCount: onProgressCount,
        isCancelled: isCancelled);
  }

  /// Syncs files for a single lesson (filtered by base name).
  /// On web this is a no-op (audio streamed directly from server).
  static Future<int> syncLesson(
    String base, {
    void Function(String message)? onProgress,
    void Function(int current, int total)? onProgressCount,
    bool Function()? isCancelled,
  }) async {
    if (kIsWeb) return 0;
    final manifest = await ApiService.getLessonManifest(base);
    return _syncManifest(manifest,
        onProgress: onProgress,
        onProgressCount: onProgressCount,
        isCancelled: isCancelled);
  }

  /// Lists locally cached lesson mp3 files. Returns empty list on web.
  static Future<List<FileSystemEntity>> listLocalMp3s() async {
    if (kIsWeb) return [];
    final dir = await _localOutputDir();
    if (!await dir.exists()) return [];
    return dir
        .listSync(recursive: true)
        .where((e) => e.path.endsWith('.mp3'))
        .toList();
  }
}
