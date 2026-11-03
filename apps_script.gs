/**
 * Приймає апрувнуте фото від бота: кладе файл у папку на Google Drive
 * і додає рядок у таблицю: Manager | Notes | Added Date | Link | Gender
 *
 * Встановлення: Таблиця «Photo» → Розширення → Apps Script → вставити цей код →
 * заповнити SECRET і FOLDER_ID → Розгорнути → Нове розгортання → Вебзастосунок
 * (Виконувати як: Я, Доступ: Усі) → скопіювати URL у SHEETS_WEBHOOK_URL.
 */

const SECRET = 'вставте-довгий-випадковий-рядок';   // той самий, що SHEETS_SECRET у .env
const FOLDER_ID = 'ID-папки-на-Google-Drive';       // з адреси папки: drive.google.com/drive/folders/<ID>
const SHEET_NAME = '';                              // порожньо = перший аркуш

function doPost(e) {
  try {
    const p = JSON.parse(e.postData.contents);
    if (p.secret !== SECRET) return json_({ ok: false, error: 'forbidden' });

    const blob = Utilities.newBlob(Utilities.base64Decode(p.data), p.mimeType, p.fileName);
    const file = DriveApp.getFolderById(FOLDER_ID).createFile(blob);

    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const sheet = SHEET_NAME ? ss.getSheetByName(SHEET_NAME) : ss.getSheets()[0];
    sheet.appendRow([
      safe_(p.manager),
      safe_(p.notes),
      new Date(),
      file.getUrl(),
      safe_(p.gender),
    ]);

    return json_({ ok: true, url: file.getUrl() });
  } catch (err) {
    return json_({ ok: false, error: String(err) });
  }
}

// Щоб текст на кшталт "=..." не став формулою
function safe_(v) {
  const s = String(v || '');
  return /^[=+\-]/.test(s) ? "'" + s : s;
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
