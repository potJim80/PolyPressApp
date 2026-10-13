// Sample states for page/index.html, for picture tests without the app:
// serve work/app over HTTP and open page/index.html?demo=<name>.
// Never shipped: build_app.sh copies page/ only.
(function () {
  const now = Date.now();
  const H = (min) => now - min * 60000;
  const name = new URLSearchParams(location.search).get("demo") || "empty";
  const base = {
    version: "Polypress 1.00 · build 794f70a", formats: ["csv", "tsv", "json", "jsonl", "parquet"], parquet: true,
    prefs: { saveMode: "ask", folder: "/Users/researcher/Documents/Archives" }, notice: "",
    staged: { compress: [], restore: [] }, jobs: [],
    history: [
      { id: "a", date: H(5), kind: "compress", src: "/Users/researcher/Data/baby_names.csv", srcName: "baby_names.csv", out: "/Users/researcher/Data/baby_names.csv.ppz", outName: "baby_names.csv.ppz", state: "done", from: 1564968, to: 54882, secs: 0.4, outExists: true, message: "" },
      { id: "b", date: H(62), kind: "restore", src: "/Users/researcher/Data/survey_2024.dta.ppz", srcName: "survey_2024.dta.ppz", out: "/Users/researcher/Data/survey_2024.dta", outName: "survey_2024.dta", state: "done", from: 812400, to: 5120000, outExists: true, message: "" },
      { id: "c", date: H(60 * 26), kind: "compress", src: "/Users/researcher/Data/export.xlsx", srcName: "export.xlsx", out: "", outName: "", state: "failed", outExists: false, message: "export.xlsx is an Excel workbook. Save it as CSV first." },
      { id: "d", date: H(60 * 24 * 9), kind: "compress", src: "/Users/researcher/Old/visits.csv", srcName: "visits.csv", out: "/Users/researcher/Old/visits.csv.ppz", outName: "visits.csv.ppz", state: "done", from: 48200000, to: 2310000, outExists: false, message: "" }
    ]
  };
  const staged = {
    compress: [
      { path: "/Users/researcher/Data/hospital_visits_2023.csv", name: "hospital_visits_2023.csv", size: 48210331 },
      { path: "/Users/researcher/Data/survey_wave3.dta", name: "survey_wave3.dta", size: 9120044 },
      { path: "/Users/researcher/Data/stations.json", name: "stations.json", size: 412003 }
    ],
    restore: [{ path: "/Users/researcher/Data/baby_names.csv.ppz", name: "baby_names.csv.ppz", size: 54882 }]
  };
  const jobs = [
    { id: 1, kind: "compress", name: "hospital_visits_2023.csv", path: "/Users/researcher/Data/hospital_visits_2023.csv", state: "done", from: 48210331, to: 2210443, secs: 3.2, out: "/Users/researcher/Archives/hospital_visits_2023.csv.ppz", outName: "hospital_visits_2023.csv.ppz", details: { rows: 50000, columns: 41, container: "modelled", made_by: "1.00" } },
    { id: 2, kind: "compress", name: "survey_wave3.dta", path: "/Users/researcher/Data/survey_wave3.dta", state: "working", note: "compressing", fraction: 0.42, startedMs: now - 6000, stageMs: now - 5000, from: 9120044 },
    { id: 3, kind: "compress", name: "stations.json", path: "/Users/researcher/Data/stations.json", state: "queued", from: 412003 },
    { id: 4, kind: "compress", name: "export.xlsx", path: "/Users/researcher/Data/export.xlsx", state: "failed", from: 82000, message: "export.xlsx is an Excel workbook. Save it as CSV first." }
  ];
  const states = {
    "empty": [base, "compress"],
    "compress-staged": [Object.assign({}, base, { staged: { compress: staged.compress, restore: [] } }), "compress"],
    "compress-working": [Object.assign({}, base, { jobs }), "compress"],
    "restore-staged": [Object.assign({}, base, { staged: { compress: [], restore: staged.restore }, prefs: { saveMode: "folder", folder: "/Users/researcher/Documents/Restored" } }), "restore"],
    "history": [base, "history"],
    "history-empty": [Object.assign({}, base, { history: [] }), "history"],
    "settings": [Object.assign({}, base, { parquet: false }), "settings"],
    "notice": [Object.assign({}, base, { notice: "A folder cannot be added. Choose the files inside it instead." }), "compress"]
  };
  const [s, t] = states[name] || states.empty;
  render(s);
  showTab(t);
})();
