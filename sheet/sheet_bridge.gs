/**
 * Quotenvergleich – Brücke zwischen eurer Google-Tabelle und dem Portal-Datenlauf (GitHub Actions)
 * ==============================================================================================
 * - GET  : liefert die Tipp-Zeilen an den Datenlauf (statt öffentlicher CSV)
 * - POST : schreibt die Quoten rechts neben eure Tipps (eure Spalten werden NICHT verändert)
 *
 * Einrichtung (einmalig):
 *   1. Tabelle öffnen → Erweiterungen → Apps Script → diesen Code einfügen → speichern
 *   2. Unten TOKEN auf ein langes, zufälliges Passwort setzen (dasselbe als GitHub-Secret SHEET_TOKEN)
 *      und BLATT_NAME auf den Namen eures Tipp-Blatts
 *   3. Bereitstellen → Neue Bereitstellung → Typ "Web-App"
 *        Ausführen als: Ich        Zugriff: Jeder
 *      → Bereitstellen, Zugriff erlauben, Web-App-URL kopieren (= GitHub-Secret SHEET_WEBAPP_URL)
 *   Ohne gültiges TOKEN liefert die Web-App nichts und schreibt nichts.
 */

// ---------------------------------------------------------------- Einstellungen
var TOKEN = 'HIER_LANGES_GEHEIMES_TOKEN_EINTRAGEN';
var BLATT_NAME = 'Tipps';          // Name des Blatts mit euren Tipps
var ERSTE_DATENZEILE = 2;          // Zeile 1 = Überschriften
var AUSGABE_AB_SPALTE = 'I';       // ab hier schreibt der Datenlauf (eure Spalten A–H bleiben unberührt)
var SPALTE_SPIEL = 'D';            // zur Kontrolle, dass die richtige Zeile beschrieben wird
var SPALTE_TIPP = 'E';
// ---------------------------------------------------------------- Ende Einstellungen

function doGet(e) {
  if (!authorized_(e && e.parameter && e.parameter.token)) return json_({ ok: false, error: 'unauthorized' });
  var sh = blatt_();
  if (!sh) return json_({ ok: false, error: 'Blatt "' + BLATT_NAME + '" nicht gefunden' });
  var last = sh.getLastRow();
  var lastCol = Math.max(colNum_(SPALTE_TIPP), colNum_(AUSGABE_AB_SPALTE) - 1);
  var rows = last >= 1 ? sh.getRange(1, 1, last, lastCol).getDisplayValues() : [];
  return json_({ ok: true, first_row: ERSTE_DATENZEILE, rows: rows, sheet: sh.getName(),
                 tz: SpreadsheetApp.getActive().getSpreadsheetTimeZone() });
}

function doPost(e) {
  var body;
  try { body = JSON.parse(e.postData.contents); } catch (err) { return json_({ ok: false, error: 'kein JSON' }); }
  if (!authorized_(body.token)) return json_({ ok: false, error: 'unauthorized' });
  var lock = LockService.getDocumentLock();
  if (!lock.tryLock(20000)) return json_({ ok: false, error: 'Tabelle gesperrt, später erneut' });
  try {
    var sh = blatt_();
    if (!sh) return json_({ ok: false, error: 'Blatt nicht gefunden' });
    var outCol = colNum_(AUSGABE_AB_SPALTE);
    var header = body.header || [];
    var width = header.length;
    if (!width) return json_({ ok: false, error: 'keine Spalten' });

    // Überschriften (nur die Ausgabe-Spalten)
    if (ERSTE_DATENZEILE > 1) {
      sh.getRange(ERSTE_DATENZEILE - 1, outCol, 1, width).setValues([header]).setFontWeight('bold');
    }
    // Aktuellen Inhalt zur Zeilenkontrolle lesen
    var last = sh.getLastRow();
    var spiel = colNum_(SPALTE_SPIEL), tipp = colNum_(SPALTE_TIPP);
    var data = last >= ERSTE_DATENZEILE
      ? sh.getRange(ERSTE_DATENZEILE, 1, last - ERSTE_DATENZEILE + 1, Math.max(spiel, tipp)).getDisplayValues() : [];
    var sigToRow = {};
    data.forEach(function (r, i) {
      var sg = sig_(r[spiel - 1], r[tipp - 1]);
      (sigToRow[sg] = sigToRow[sg] || []).push(ERSTE_DATENZEILE + i);
    });

    var written = 0, moved = 0, skipped = [];
    (body.rows || []).forEach(function (item) {
      var row = Number(item.row);
      var idx = row - ERSTE_DATENZEILE;
      var ok = idx >= 0 && idx < data.length && sig_(data[idx][spiel - 1], data[idx][tipp - 1]) === item.sig;
      if (!ok) {                                   // Zeilen verschoben? -> per Spiel+Tipp suchen
        var cand = sigToRow[item.sig] || [];
        if (cand.length === 1) { row = cand[0]; ok = true; moved++; }
      }
      if (!ok) { skipped.push(item.row); return; }
      var vals = (item.values || []).slice(0, width);
      while (vals.length < width) vals.push('');
      sh.getRange(row, outCol, 1, width).setValues([vals]);
      written++;
    });
    formatieren_(sh, outCol, width, body.layout || {});
    return json_({ ok: true, written: written, moved: moved, skipped: skipped });
  } finally {
    lock.releaseLock();
  }
}

// ---------------------------------------------------------------- Formatierung
function formatieren_(sh, outCol, width, layout) {
  var n = Math.max(1, sh.getLastRow() - ERSTE_DATENZEILE + 1);
  var first = ERSTE_DATENZEILE;
  var mine = function (r) { return r.getRanges().some(function (g) { return g.getColumn() >= outCol && g.getColumn() < outCol + width; }); };
  var rules = sh.getConditionalFormatRules().filter(function (r) { return !mine(r); });
  if (layout.reach_col != null) {
    var rg = sh.getRange(first, outCol + layout.reach_col, n, 1);
    rules.push(SpreadsheetApp.newConditionalFormatRule().whenTextStartsWith('JA').setBackground('#c8e6c9').setRanges([rg]).build());
    rules.push(SpreadsheetApp.newConditionalFormatRule().whenTextStartsWith('NEIN').setBackground('#ffcdd2').setRanges([rg]).build());
  }
  if (layout.value_col != null) {
    var vr = sh.getRange(first, outCol + layout.value_col, n, 1);
    rules.push(SpreadsheetApp.newConditionalFormatRule().whenNumberGreaterThan(0).setBackground('#c8e6c9').setRanges([vr]).build());
  }
  if (layout.books_from != null && layout.books_count) {
    var bc = outCol + layout.books_from, nb = layout.books_count;
    var a1 = colLetter_(bc) + first, span = '$' + colLetter_(bc) + first + ':$' + colLetter_(bc + nb - 1) + first;
    rules.push(SpreadsheetApp.newConditionalFormatRule()
      .whenFormulaSatisfied('=AND(ISNUMBER(' + a1 + '),' + a1 + '=MAX(' + span + '))')
      .setBackground('#f6cf3c').setBold(true).setRanges([sh.getRange(first, bc, n, nb)]).build());
  }
  sh.setConditionalFormatRules(rules);
}

// ---------------------------------------------------------------- Hilfen
function authorized_(t) {
  return TOKEN && TOKEN.indexOf('HIER_') !== 0 && TOKEN.length >= 16 && String(t || '') === TOKEN;
}
function blatt_() { return SpreadsheetApp.getActive().getSheetByName(BLATT_NAME); }
function json_(o) { return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON); }
function sig_(game, tip) {
  var n = function (s) { return String(s || '').toLowerCase().normalize('NFKD').replace(/[̀-ͯ]/g, '').replace(/[^a-z0-9]/g, ''); };
  return n(game) + '|' + n(tip);
}
function colNum_(letters) {
  var s = String(letters).trim().toUpperCase(), n = 0;
  for (var i = 0; i < s.length; i++) n = n * 26 + (s.charCodeAt(i) - 64);
  return n;
}
function colLetter_(n) { var s = ''; while (n > 0) { var m = (n - 1) % 26; s = String.fromCharCode(65 + m) + s; n = Math.floor((n - 1) / 26); } return s; }

/** Zum Testen im Editor: zeigt, wie viele Tipp-Zeilen gefunden werden. */
function testLesen() {
  var sh = blatt_();
  Logger.log(sh ? 'Blatt gefunden, ' + Math.max(0, sh.getLastRow() - ERSTE_DATENZEILE + 1) + ' Zeilen' : 'Blatt "' + BLATT_NAME + '" fehlt');
}
