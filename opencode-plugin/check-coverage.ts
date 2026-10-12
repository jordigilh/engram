const reportPath = "coverage/lcov.info"
const report = await Bun.file(reportPath).text().catch(() => "")
const lineTotals = [...report.matchAll(/^LF:(\d+)$/gm)].map((match) => Number(match[1]))
const lineHits = [...report.matchAll(/^LH:(\d+)$/gm)].map((match) => Number(match[1]))
const totalLines = lineTotals.reduce((sum, value) => sum + value, 0)
const coveredLines = lineHits.reduce((sum, value) => sum + value, 0)
const percentage = totalLines === 0 ? 0 : (coveredLines / totalLines) * 100

console.log(`Bun line coverage: ${percentage.toFixed(2)}% (${coveredLines}/${totalLines})`)
if (percentage < 90) {
  console.error("Bun line coverage must be at least 90%")
  process.exit(1)
}
