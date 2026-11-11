import 'dart:convert';
import 'package:sqflite/sqflite.dart';
import 'package:path/path.dart';

class LocalDbService {
  static Database? _db;

  static Future<Database> get db async {
    _db ??= await _open();
    return _db!;
  }

  static Future<Database> _open() async {
    final path = join(await getDatabasesPath(), 'lingodiary.db');
    return openDatabase(
      path,
      version: 4,
      onCreate: (db, version) async {
        await _createV1Tables(db);
        await _createV2Tables(db);
        await _createV3Tables(db);
        await _createV4Tables(db);
      },
      onUpgrade: (db, oldVersion, newVersion) async {
        if (oldVersion < 2) {
          await _createV2Tables(db);
        }
        if (oldVersion < 3) {
          await _createV3Tables(db);
        }
        if (oldVersion < 4) {
          await _createV4Tables(db);
        }
      },
    );
  }

  static Future<void> _createV1Tables(Database db) async {
    await db.execute('''
      CREATE TABLE IF NOT EXISTS diary_cache (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content TEXT NOT NULL,
        saved_at TEXT NOT NULL,
        synced INTEGER NOT NULL DEFAULT 0
      )
    ''');
    await db.execute('''
      CREATE TABLE IF NOT EXISTS lessons_cache (
        base TEXT PRIMARY KEY,
        display TEXT NOT NULL,
        variants_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
      )
    ''');
  }

  static Future<void> _createV2Tables(Database db) async {
    await db.execute('''
      CREATE TABLE IF NOT EXISTS home_cache (
        id INTEGER PRIMARY KEY,
        data_json TEXT NOT NULL,
        fetched_at TEXT NOT NULL
      )
    ''');
    await db.execute('''
      CREATE TABLE IF NOT EXISTS srs_scores (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        entry_index INTEGER NOT NULL,
        score INTEGER NOT NULL,
        scored_at TEXT NOT NULL,
        synced INTEGER NOT NULL DEFAULT 0
      )
    ''');
    await db.execute('''
      CREATE TABLE IF NOT EXISTS lesson_last_reviewed (
        date TEXT PRIMARY KEY,
        last_reviewed TEXT NOT NULL,
        synced INTEGER NOT NULL DEFAULT 0
      )
    ''');
  }

  static Future<void> _createV3Tables(Database db) async {
    await db.execute('''
      CREATE TABLE IF NOT EXISTS day_cache (
        date TEXT PRIMARY KEY,
        day_json TEXT NOT NULL,
        fetched_at TEXT NOT NULL
      )
    ''');
  }

  static Future<void> _createV4Tables(Database db) async {
    await db.execute('''
      CREATE TABLE IF NOT EXISTS pending_trials (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        trials_json TEXT NOT NULL,
        synced INTEGER NOT NULL DEFAULT 0
      )
    ''');
  }

  // ---- Diary ----

  static Future<void> saveDiaryContent(String content) async {
    final database = await db;
    await database.insert('diary_cache', {
      'content': content,
      'saved_at': DateTime.now().toIso8601String(),
      'synced': 0,
    });
  }

  static Future<String?> getLatestDiaryContent() async {
    final database = await db;
    final rows = await database.query(
      'diary_cache',
      orderBy: 'id DESC',
      limit: 1,
    );
    if (rows.isEmpty) return null;
    return rows.first['content'] as String?;
  }

  static Future<void> markDiarySynced(int id) async {
    final database = await db;
    await database.update('diary_cache', {'synced': 1},
        where: 'id = ?', whereArgs: [id]);
  }

  /// Returns all unsynced diary entries as structured {id, date, sentences} maps.
  /// Content format expected: "[YYYY-MM-DD]\n- sentence1\n- sentence2"
  static Future<List<Map<String, dynamic>>> getUnsyncedDiaryEntries() async {
    final database = await db;
    final rows = await database.query('diary_cache',
        where: 'synced = 0', orderBy: 'id ASC');
    final result = <Map<String, dynamic>>[];
    for (final row in rows) {
      final content = row['content'] as String? ?? '';
      // Extract date from "[YYYY-MM-DD]" header line.
      final dateMatch = RegExp(r'\[(\d{4}-\d{2}-\d{2})\]').firstMatch(content);
      if (dateMatch == null) continue;
      final date = dateMatch.group(1)!;
      // Extract sentences from "- sentence" lines.
      final sentences = content
          .split('\n')
          .where((l) => l.startsWith('- '))
          .map((l) => l.substring(2).trim())
          .where((s) => s.isNotEmpty)
          .toList();
      if (sentences.isEmpty) continue;
      result
          .add({'id': row['id'] as int, 'date': date, 'sentences': sentences});
    }
    return result;
  }

  // ---- Lessons ----

  static Future<void> saveLessons(List<Map<String, dynamic>> lessons) async {
    final database = await db;
    final batch = database.batch();
    for (final lesson in lessons) {
      batch.insert(
        'lessons_cache',
        {
          'base': lesson['base'],
          'display': lesson['display'],
          'variants_json': jsonEncode(lesson['variants']),
          'updated_at': DateTime.now().toIso8601String(),
        },
        conflictAlgorithm: ConflictAlgorithm.replace,
      );
    }
    await batch.commit(noResult: true);
  }

  static Future<List<Map<String, dynamic>>> getLessons() async {
    final database = await db;
    final List<Map<String, dynamic>> rows =
        await database.query('lessons_cache', orderBy: 'display DESC');

    // Map the raw SQFlite rows to editable Maps and decode the JSON payload
    return rows.map((row) {
      final mutableMap = Map<String, dynamic>.from(row);

      if (mutableMap['variants_json'] != null) {
        try {
          // Un-marshal the raw SQLite string back into a standard Dart Map structure
          mutableMap['variants'] =
              jsonDecode(mutableMap['variants_json'] as String);
        } catch (_) {
          mutableMap['variants'] = <String, dynamic>{};
        }
      }

      return mutableMap;
    }).toList();
  }
  // ---- Day cache ----

  static Future<void> saveDayCache(
      String date, Map<String, dynamic> dayObject) async {
    final database = await db;
    await database.insert(
      'day_cache',
      {
        'date': date,
        'day_json': jsonEncode(dayObject),
        'fetched_at': DateTime.now().toIso8601String(),
      },
      conflictAlgorithm: ConflictAlgorithm.replace,
    );
  }

  static Future<Map<String, dynamic>?> getDayCache(String date) async {
    final database = await db;
    final rows =
        await database.query('day_cache', where: 'date = ?', whereArgs: [date]);
    if (rows.isEmpty) return null;
    try {
      return jsonDecode(rows.first['day_json'] as String)
          as Map<String, dynamic>;
    } catch (_) {
      return null;
    }
  }

  static Future<List<String>> getAllCachedDates() async {
    final database = await db;
    final rows = await database.query('day_cache', columns: ['date']);
    return rows.map((r) => r['date'] as String).toList();
  }

  // ---- Home cache ----

  static Future<void> saveHomeData(Map<String, dynamic> data) async {
    final database = await db;
    await database.insert(
      'home_cache',
      {
        'id': 1,
        'data_json': jsonEncode(data),
        'fetched_at': DateTime.now().toIso8601String(),
      },
      conflictAlgorithm: ConflictAlgorithm.replace,
    );
  }

  static Future<Map<String, dynamic>?> getCachedHomeData() async {
    final database = await db;
    final rows = await database.query('home_cache', where: 'id = 1');
    if (rows.isEmpty) return null;
    try {
      return jsonDecode(rows.first['data_json'] as String)
          as Map<String, dynamic>;
    } catch (_) {
      return null;
    }
  }

  // ---- SRS scores ----

  static Future<int> saveSrsScore({
    required String date,
    required int entryIndex,
    required int score,
    bool synced = false,
  }) async {
    final database = await db;
    return database.insert('srs_scores', {
      'date': date,
      'entry_index': entryIndex,
      'score': score,
      'scored_at': DateTime.now().toIso8601String(),
      'synced': synced ? 1 : 0,
    });
  }

  static Future<List<Map<String, dynamic>>> getUnsyncedScores() async {
    final database = await db;
    return database.query('srs_scores', where: 'synced = 0', orderBy: 'id ASC');
  }

  static Future<void> markScoreSynced(int id) async {
    final database = await db;
    await database.update('srs_scores', {'synced': 1},
        where: 'id = ?', whereArgs: [id]);
  }

  static Future<void> resetScoreSynced(int id) async {
    final database = await db;
    await database.update('srs_scores', {'synced': 0},
        where: 'id = ?', whereArgs: [id]);
  }

  /// Deletes old synced rows from outbox tables.
  ///
  /// `srs_scores` is intentionally NOT pruned: [getReviewStats] derives the
  /// total, score distribution and streak from it.
  static Future<void> cleanupOldSyncedRows({int days = 90}) async {
    final database = await db;
    final cutoff =
        DateTime.now().subtract(Duration(days: days)).toIso8601String();
    await database.delete('diary_cache',
        where: 'synced = 1 AND saved_at < ?', whereArgs: [cutoff]);
    await database.delete('pending_trials',
        where: 'synced = 1');
  }

  /// Returns review statistics for the stats/streak screen.
  static Future<Map<String, dynamic>> getReviewStats() async {
    final database = await db;

    // Total reviews
    final totalResult =
        await database.rawQuery('SELECT COUNT(*) as c FROM srs_scores');
    final total = (totalResult.first['c'] as int?) ?? 0;

    // Today's reviews (using local time)
    final todayResult = await database.rawQuery(
      "SELECT COUNT(*) as c FROM srs_scores "
      "WHERE date(scored_at, 'localtime') = date('now', 'localtime')",
    );
    final todayCount = (todayResult.first['c'] as int?) ?? 0;

    // Score distribution
    final distResult = await database.rawQuery(
      'SELECT score, COUNT(*) as c FROM srs_scores GROUP BY score ORDER BY score',
    );
    final distribution = <int, int>{};
    for (final row in distResult) {
      distribution[(row['score'] as int?) ?? 0] = (row['c'] as int?) ?? 0;
    }

    // Streak: fetch all distinct review days, ordered descending
    final daysResult = await database.rawQuery(
      "SELECT DISTINCT date(scored_at, 'localtime') as d "
      'FROM srs_scores ORDER BY d DESC',
    );
    final days = daysResult.map((r) => r['d'] as String).toList();

    int streak = 0;
    if (days.isNotEmpty) {
      final today = DateTime.now();
      final todayStr =
          '${today.year}-${today.month.toString().padLeft(2, '0')}-${today.day.toString().padLeft(2, '0')}';
      final yesterdayDt = today.subtract(const Duration(days: 1));
      final yesterdayStr =
          '${yesterdayDt.year}-${yesterdayDt.month.toString().padLeft(2, '0')}-${yesterdayDt.day.toString().padLeft(2, '0')}';

      // Streak starts only if reviewed today or yesterday
      if (days.first == todayStr || days.first == yesterdayStr) {
        DateTime cursor = days.first == todayStr ? today : yesterdayDt;
        for (final d in days) {
          final curStr =
              '${cursor.year}-${cursor.month.toString().padLeft(2, '0')}-${cursor.day.toString().padLeft(2, '0')}';
          if (d == curStr) {
            streak++;
            cursor = cursor.subtract(const Duration(days: 1));
          } else {
            break;
          }
        }
      }
    }

    return {
      'total': total,
      'today': todayCount,
      'streak': streak,
      'distribution': distribution,
    };
  }

  /// Deletes all rows from all cache tables.
  static Future<void> clearAll() async {
    final database = await db;
    await database.delete('diary_cache');
    await database.delete('lessons_cache');
    await database.delete('home_cache');
    await database.delete('srs_scores');
    await database.delete('lesson_last_reviewed');
    await database.delete('day_cache');
    await database.delete('pending_trials');
  }

  // ---- Lesson last_reviewed tracking ----

  static Future<void> saveLessonLastReviewed(
      String date, String timestamp) async {
    final database = await db;
    await database.insert(
      'lesson_last_reviewed',
      {
        'date': date,
        'last_reviewed': timestamp,
        'synced': 0,
      },
      conflictAlgorithm: ConflictAlgorithm.replace,
    );
  }

  static Future<List<Map<String, dynamic>>> getUnsyncedLessonReviews() async {
    final database = await db;
    return database.query('lesson_last_reviewed', where: 'synced = 0');
  }

  static Future<void> markLessonReviewSynced(String date) async {
    final database = await db;
    await database.update('lesson_last_reviewed', {'synced': 1},
        where: 'date = ?', whereArgs: [date]);
  }

  static Future<List<Map<String, dynamic>>> getRecentlyStudiedLessons(
      {int limit = 10}) async {
    final database = await db;
    return database.query(
      'lesson_last_reviewed',
      orderBy: 'last_reviewed DESC',
      limit: limit,
    );
  }

  // ---- Translation attempts ----

  static Future<void> savePendingTrials(
      String date, List<String> trials) async {
    final database = await db;
    await database.insert('pending_trials', {
      'date': date,
      'trials_json': jsonEncode(trials),
      'synced': 0,
    });
  }

  static Future<List<Map<String, dynamic>>> getUnsyncedTrials() async {
    final database = await db;
    final rows = await database.query('pending_trials',
        where: 'synced = 0', orderBy: 'id ASC');
    return rows
        .map((r) => {
              'id': r['id'] as int,
              'date': r['date'] as String,
              'trials': (jsonDecode(r['trials_json'] as String) as List)
                  .cast<String>(),
            })
        .toList();
  }

  static Future<void> markTrialSynced(int id) async {
    final database = await db;
    await database.update('pending_trials', {'synced': 1},
        where: 'id = ?', whereArgs: [id]);
  }
}
