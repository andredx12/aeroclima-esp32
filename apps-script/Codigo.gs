// Apps Script vinculado à planilha (Extensões > Apps Script), publicado como App da Web.
// O ESP32 chama a URL /exec com os dados na query string e cada chamada vira uma linha nova.
function doGet(e) {
  const aba = SpreadsheetApp.getActiveSpreadsheet().getSheetByName('Leituras');
  const p = e.parameter;
  aba.appendRow([
    new Date(),
    Number(p.temp),
    Number(p.umid),
    Number(p.lux),
    Number(p.gas),
    p.qualidade,
    Number(p.idt),
    p.conforto
  ]);
  return ContentService.createTextOutput('ok');
}
