import 'dart:async';
import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';
import 'api_service.dart';
import 'local_db_service.dart';
import 'sync_service.dart';

/// Global singleton that manages sync state across all screens.
/// Start a sync from any screen; progress is visible app-wide.
class SyncManager extends ChangeNotifier {
  SyncManager._();
  static final instance = SyncManager._();

  bool isSyncing = false;
  bool _cancelled = false;
  bool _flushing = false;
  bool _cleanedUp = false;
  double? progress; // null = indeterminate, 0.0–1.0 = determinate
  String message = '';
  int filesDownloaded = 0;

  /// Whether the server is currently reachable.
  bool isOnline = false;
  bool _wasOnline = false;
  Timer? _connectivityTimer;

  /// Starts a periodic server-reachability check (every 15 s).
  /// On Android: pings the configured server_url from SharedPreferences.
  /// On web: pings the same origin the page was served from (server is co-located).
  void startConnectivityWatch() {
    _connectivityTimer?.cancel();
    _checkConnectivity(); // immediate first check
    _connectivityTimer = Timer.periodic(
      const Duration(seconds: 15),
      (_) => _checkConnectivity(),
    );
  }

  Future<void> _checkConnectivity() async {
    try {
      final String base;
      if (kIsWeb) {
        // On web the Flutter app is served by the same Flask server, so we
        // ping our own origin — no SharedPreferences server_url needed.
        base = Uri.base.origin;
      } else {
        final prefs = await SharedPreferences.getInstance();
        final serverUrl = prefs.getString('server_url') ?? '';
        if (serverUrl.isEmpty) {
          _setOnline(false);
          return;
        }
        base = serverUrl.endsWith('/')
            ? serverUrl.substring(0, serverUrl.length - 1)
            : serverUrl;
      }
      await http
          .get(Uri.parse('$base/api/config'))  // any HTTP answer (even 401) means reachable
          .timeout(const Duration(seconds: 5));
      _setOnline(true);
    } catch (_) {
      _setOnline(false);
    }
  }

  void _setOnline(bool value) {
    _wasOnline = isOnline;
    isOnline = value;
    if (_wasOnline != value) notifyListeners();
    if (!_wasOnline && value) {
      // Just came back online — flush pending queue then clean up old rows.
      flushPending();
    }
    if (value && !_cleanedUp) {
      _cleanedUp = true;
      LocalDbService.cleanupOldSyncedRows();
    }
  }

  void cancel() {
    _cancelled = true;
  }

  /// True when the failure means the server can't be reached (retry later).
  static bool _isNetworkError(Object e) {
    if (e is TimeoutException) return true;
    if (e is ApiException) return e.statusCode >= 500 || e.statusCode == 401;
    // `http` wraps socket failures in a ClientException on some platforms.
    final text = e.toString();
    return text.contains('SocketException') ||
        text.contains('ClientException') ||
        text.contains('Connection refused') ||
        text.contains('Failed host lookup');
  }

  /// True when the server understood the request and rejected it for good
  /// (e.g. 400/404), so retrying the same queued item can never succeed.
  static bool _isPermanentRejection(Object e) =>
      e is ApiException && e.statusCode >= 400 && e.statusCode < 500 && e.statusCode != 401 && e.statusCode != 429;

  /// Flushes pending scores, diary entries, lesson reviews, and translation trials to the server.
  /// Safe to call concurrently — a second call is ignored while one is running.
  Future<void> flushPending() async {
    if (_flushing) return;
    _flushing = true;
    // A user cancelling a download must not permanently disable the outbox.
    if (!isSyncing) _cancelled = false;
    try {
      await _syncPendingLessonReviews();
      await _syncPendingDiaryEntries();
      await _syncPendingScores();
      await _syncPendingTrials();
    } finally {
      _flushing = false;
    }
  }

  Future<void> syncLesson(String base, {String? date}) async {
    if (isSyncing) return;
    _start('Loading manifest…');
    try {
      await flushPending();
      final count = await SyncService.syncLesson(
        base,
        onProgress: (msg) {
          message = msg;
          notifyListeners();
        },
        onProgressCount: (current, total) {
          progress = total > 0 ? current / total : 1.0;
          message = '$current / $total';
          notifyListeners();
        },
        isCancelled: () => _cancelled,
      );
      filesDownloaded = count;
      message = _cancelled ? 'Cancelled.' : 'Synced $count file(s) ✓';
      progress = 1.0;
      notifyListeners();

      // After successful sync, populate day_cache if date is known
      if (date != null && !_cancelled) {
        try {
          final dayData = await ApiService.getDayData(date);
          await LocalDbService.saveDayCache(date, dayData);
        } catch (_) {
          // Non-fatal — lesson text will be loaded on next player open
        }
      }
    } catch (e) {
      message = 'Sync failed: $e';
      progress = null;
      notifyListeners();
    } finally {
      isSyncing = false;
      notifyListeners();
    }
  }

  Future<void> syncAll({void Function(String)? onProgress}) async {
    if (isSyncing) return;
    _start('Loading manifest…');
    try {
      await flushPending();
      if (kIsWeb) {
        // Web: audio streams directly from server, no local caching needed.
        message = 'Web mode — audio streams from server ✓';
        progress = 1.0;
        notifyListeners();
      } else {
        final count = await SyncService.syncFromServer(
          onProgress: (msg) {
            message = msg;
            notifyListeners();
          },
          onProgressCount: (current, total) {
            progress = total > 0 ? current / total : 1.0;
            message = '$current / $total';
            notifyListeners();
          },
          isCancelled: () => _cancelled,
        );
        filesDownloaded = count;
        message = _cancelled ? 'Cancelled.' : 'Synced $count file(s) ✓';
        progress = 1.0;
        notifyListeners();

        // After successful sync, populate day_cache for all lessons
        if (!_cancelled) {
          try {
            message = 'Caching lesson text…';
            notifyListeners();
            final lessons = await ApiService.getLessons();
            for (final lesson in lessons) {
              if (_cancelled) break;
              final date = lesson['date'] as String?;
              if (date == null || date.isEmpty) continue;
              try {
                final dayData = await ApiService.getDayData(date);
                await LocalDbService.saveDayCache(date, dayData);
              } catch (_) {
                // Non-fatal — skip this day
              }
            }
          } catch (_) {
            // Non-fatal — text cache population failed, will retry on lesson open
          }
        }
      }
    } catch (e) {
      message = 'Sync failed: $e';
      progress = null;
      notifyListeners();
    } finally {
      isSyncing = false;
      notifyListeners();
    }
  }

  void _start(String initialMessage) {
    isSyncing = true;
    _cancelled = false;
    progress = null;
    message = initialMessage;
    filesDownloaded = 0;
    notifyListeners();
  }

  /// Sync pending lesson last_reviewed timestamps to server
  Future<void> _syncPendingLessonReviews() async {
    final pending = await LocalDbService.getUnsyncedLessonReviews();
    for (final row in pending) {
      if (_cancelled) break;
      final date = row['date'] as String;
      try {
        final timestamp = row['last_reviewed'] as String;
        await ApiService.updateLessonLastReviewed(date, timestamp: timestamp);
        await LocalDbService.markLessonReviewSynced(date);
      } catch (e) {
        if (_isPermanentRejection(e)) {
          debugPrint('SyncManager: dropping rejected lesson review $date: $e');
          await LocalDbService.markLessonReviewSynced(date);
          continue;
        }
        if (_isNetworkError(e)) break; // retry on next sync
        debugPrint('SyncManager: lesson review sync failed for ${row['date']}: $e');
      }
    }
  }

  /// Replay any locally saved scores that weren't sent to the server yet.
  /// Continues past individual failures so all pending scores are attempted.
  Future<void> _syncPendingScores() async {
    final pending = await LocalDbService.getUnsyncedScores();
    for (final row in pending) {
      if (_cancelled) break;
      try {
        await ApiService.scoreEntry(
          row['date'] as String,
          row['entry_index'] as int,
          row['score'] as int,
          scoredAt: DateTime.tryParse(row['scored_at'] as String? ?? ''),
        );
        await LocalDbService.markScoreSynced(row['id'] as int);
      } catch (e) {
        if (_isPermanentRejection(e)) {
          // e.g. the entry no longer exists: drop it instead of retrying forever.
          debugPrint('SyncManager: dropping rejected score ${row['id']}: $e');
          await LocalDbService.markScoreSynced(row['id'] as int);
          continue;
        }
        // Network unreachable or other error — no point continuing, try again on next sync.
        if (_isNetworkError(e)) break; // retry on next sync
        // Log but continue so remaining scores are still attempted.
        debugPrint('SyncManager: score sync failed for row ${row['id']}: $e');
      }
    }
  }

  /// Replay any locally saved diary entries that weren't sent to the server yet.
  Future<void> _syncPendingDiaryEntries() async {
    final pending = await LocalDbService.getUnsyncedDiaryEntries();
    for (final row in pending) {
      if (_cancelled) break;
      final id = row['id'] as int;
      final date = row['date'] as String;
      final sentences = row['sentences'] as List<String>;
      try {
        await ApiService.addSentences(date, sentences);
        await LocalDbService.markDiarySynced(id);
      } catch (e) {
        if (_isPermanentRejection(e)) {
          debugPrint('SyncManager: dropping rejected diary entry $id: $e');
          await LocalDbService.markDiarySynced(id);
          continue;
        }
        if (_isNetworkError(e)) break; // retry on next sync
        debugPrint('SyncManager: diary entry sync failed for row $id: $e');
      }
    }
  }

  /// Flush pending translation attempts to the server.
  Future<void> _syncPendingTrials() async {
    final pending = await LocalDbService.getUnsyncedTrials();
    for (final row in pending) {
      if (_cancelled) break;
      final id = row['id'] as int;
      final date = row['date'] as String;
      final trials = row['trials'] as List<String>;
      try {
        await ApiService.saveTrials(date, trials);
        await LocalDbService.markTrialSynced(id);
      } catch (e) {
        if (_isPermanentRejection(e)) {
          debugPrint('SyncManager: dropping rejected trials $id: $e');
          await LocalDbService.markTrialSynced(id);
          continue;
        }
        if (_isNetworkError(e)) break; // retry on next sync
        debugPrint('SyncManager: trial sync failed for row $id: $e');
      }
    }
  }
}
