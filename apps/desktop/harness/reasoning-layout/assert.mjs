import { mkdir } from 'node:fs/promises'
import { dirname } from 'node:path'

import { chromium } from '@playwright/test'

const url = process.argv[2] ?? 'http://127.0.0.1:5174/harness/reasoning-layout/'
const screenshotPath = process.argv[3]
const commentaryText = 'I’m checking the persisted turn now.'
const expectedReasoningTexts = [
  'Analyzing Pi core cut selection algorithm',
  'Clarifying cut point handling in tool results',
  'Examining payload rewrite excluding pre-compaction kept window',
  'Verifying serializer handling of compacted data'
]
const thoughtDensityTexts = [
  'Thought density paragraph one.',
  'Thought density paragraph two.',
  'Thought density paragraph three.'
]
const ordinaryProseTexts = [
  'Ordinary prose paragraph one.',
  'Ordinary prose paragraph two.',
  'Ordinary prose paragraph three.'
]
const browser = await chromium.launch({ headless: true })
const page = await browser.newPage({ viewport: { width: 900, height: 700 } })
page.setDefaultTimeout(120_000)

const readRectangle = locator =>
  locator.evaluate(node => {
    const rect = node.getBoundingClientRect()
    return {
      top: rect.top,
      right: rect.right,
      bottom: rect.bottom,
      left: rect.left,
      width: rect.width,
      height: rect.height
    }
  })

const readParagraphMetrics = async locator => {
  const paragraphs = locator.locator('p')
  const texts = await paragraphs.allTextContents()
  const rectangles = await paragraphs.evaluateAll(nodes =>
    nodes.map(node => {
      const rect = node.getBoundingClientRect()
      return { top: rect.top, bottom: rect.bottom, height: rect.height }
    })
  )
  return {
    texts: texts.map(text => text.trim()),
    rectangles,
    gaps: rectangles.slice(1).map((rectangle, index) => rectangle.top - rectangles[index].bottom),
    pitches: rectangles.slice(1).map((rectangle, index) => rectangle.top - rectangles[index].top)
  }
}

try {
  await page.goto(url, { timeout: 120_000, waitUntil: 'domcontentloaded' })
  const disclosures = page.locator('[data-slot="aui_thinking-disclosure"]')
  const disclosureCount = await disclosures.count()
  if (disclosureCount !== 2) {
    throw new Error(`Expected Task 5 and density-control Thought disclosures, found ${disclosureCount}`)
  }
  for (const disclosure of await disclosures.all()) {
    await disclosure.getByRole('button').click()
  }

  const disclosure = disclosures.filter({ hasText: expectedReasoningTexts[0] })
  const densityDisclosure = disclosures.filter({ hasText: thoughtDensityTexts[0] })
  const parts = disclosure.locator('[data-slot="aui_reasoning-text"]')
  const count = await parts.count()
  const rows = await parts.evaluateAll(nodes =>
    nodes.map(node => {
      const rect = node.getBoundingClientRect()
      return {
        display: getComputedStyle(node).display,
        text: node.textContent?.trim() ?? '',
        top: rect.top
      }
    })
  )

  if (count !== 4) {
    throw new Error(`Expected 4 reasoning items, found ${count}: ${JSON.stringify(rows)}`)
  }

  if (rows.some(row => row.display !== 'block')) {
    throw new Error(`Every reasoning item must explicitly render as display:block: ${JSON.stringify(rows)}`)
  }

  if (new Set(rows.map(row => Math.round(row.top))).size !== rows.length) {
    throw new Error(`Reasoning items do not occupy distinct visual rows: ${JSON.stringify(rows)}`)
  }

  const reasoningTexts = rows.map(row => row.text)
  if (JSON.stringify(reasoningTexts) !== JSON.stringify(expectedReasoningTexts)) {
    throw new Error(
      `Reasoning summary order changed: expected ${JSON.stringify(expectedReasoningTexts)}, found ${JSON.stringify(reasoningTexts)}`
    )
  }

  const nativeSummaryPitches = rows.slice(1).map((row, index) => row.top - rows[index].top)
  const thoughtDensityBlock = densityDisclosure.locator('[data-slot="aui_reasoning-text"]')
  const thoughtMetrics = await readParagraphMetrics(thoughtDensityBlock)
  if (JSON.stringify(thoughtMetrics.texts) !== JSON.stringify(thoughtDensityTexts)) {
    throw new Error(
      `Thought density fixture must render three Markdown paragraphs: expected ${JSON.stringify(thoughtDensityTexts)}, found ${JSON.stringify(thoughtMetrics.texts)}`
    )
  }
  if (thoughtMetrics.gaps.some(gap => gap < -0.5)) {
    throw new Error(`Thought paragraphs overlap: ${JSON.stringify(thoughtMetrics)}`)
  }

  const assistantMarkdownBlocks = page.locator('[data-role="assistant"] .aui-md')
  const ordinaryProseBlock = assistantMarkdownBlocks
    .filter({ has: page.locator('p', { hasText: ordinaryProseTexts[0] }) })
    .first()
  const ordinaryMetrics = await readParagraphMetrics(ordinaryProseBlock)
  if (JSON.stringify(ordinaryMetrics.texts) !== JSON.stringify(ordinaryProseTexts)) {
    throw new Error(
      `Ordinary prose control must render three Markdown paragraphs: expected ${JSON.stringify(ordinaryProseTexts)}, found ${JSON.stringify(ordinaryMetrics.texts)}`
    )
  }
  const expectedOrdinaryGap = await ordinaryProseBlock.evaluate(node => {
    const value = getComputedStyle(node).getPropertyValue('--paragraph-gap').trim()
    const probe = document.createElement('div')
    probe.style.position = 'absolute'
    probe.style.width = value
    node.append(probe)
    const pixels = probe.getBoundingClientRect().width
    probe.remove()
    return pixels
  })
  if (ordinaryMetrics.gaps.some(gap => Math.abs(gap - expectedOrdinaryGap) > 0.75)) {
    throw new Error(
      `Ordinary assistant prose must retain --paragraph-gap (${expectedOrdinaryGap}px): ${JSON.stringify(ordinaryMetrics)}`
    )
  }

  if (screenshotPath) {
    await mkdir(dirname(screenshotPath), { recursive: true })
    await page.screenshot({ path: screenshotPath, fullPage: true })
  }

  const nativePitch = nativeSummaryPitches.reduce((sum, pitch) => sum + pitch, 0) / nativeSummaryPitches.length
  const thoughtPitch = thoughtMetrics.pitches.reduce((sum, pitch) => sum + pitch, 0) / thoughtMetrics.pitches.length
  if (Math.abs(thoughtPitch - nativePitch) > 1) {
    throw new Error(
      `Thought paragraph pitch is not compact: ${thoughtPitch}px versus native-summary row pitch ${nativePitch}px; gaps=${JSON.stringify(thoughtMetrics.gaps)}`
    )
  }

  const commentaryBlocks = assistantMarkdownBlocks.filter({
    has: page.locator('p', { hasText: commentaryText })
  })
  const exactCommentaryBlocks = commentaryBlocks.filter({ hasText: commentaryText })
  const exactCommentaryCount = await exactCommentaryBlocks.evaluateAll(
    (nodes, expected) => nodes.filter(node => node.textContent?.trim() === expected).length,
    commentaryText
  )

  if (exactCommentaryCount !== 1) {
    throw new Error(`Expected exact commentary block once, found ${exactCommentaryCount}: ${commentaryText}`)
  }

  const commentary = exactCommentaryBlocks.filter({ hasText: commentaryText }).first()
  if ((await commentary.textContent())?.trim() !== commentaryText) {
    throw new Error(`Commentary must occupy one exact assistant .aui-md text block: ${commentaryText}`)
  }

  const disclosureCommentaryCount = await disclosure.locator('.aui-md').evaluateAll(
    (nodes, expected) => nodes.filter(node => node.textContent?.trim() === expected).length,
    commentaryText
  )
  if (disclosureCommentaryCount !== 0) {
    throw new Error(`Commentary rendered inside the Thought disclosure: ${disclosureCommentaryCount}`)
  }

  const commentaryIsDescendant = await commentary.evaluate(
    (node, disclosureNode) => disclosureNode.contains(node),
    await disclosure.elementHandle()
  )
  if (commentaryIsDescendant) {
    throw new Error('Commentary must not be a descendant of the Thought disclosure')
  }

  const disclosureRectangle = await readRectangle(disclosure)
  const commentaryRectangle = await readRectangle(commentary)
  const commentaryDisplay = await commentary.evaluate(node => getComputedStyle(node).display)
  const commentaryVisible = await commentary.evaluate(node => {
    const style = getComputedStyle(node)
    const rect = node.getBoundingClientRect()
    return style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity) > 0 && rect.width > 0 && rect.height > 0
  })
  const rectanglesOverlap = !(
    commentaryRectangle.left >= disclosureRectangle.right ||
    commentaryRectangle.right <= disclosureRectangle.left ||
    commentaryRectangle.top >= disclosureRectangle.bottom ||
    commentaryRectangle.bottom <= disclosureRectangle.top
  )
  const commentaryOutside = !commentaryIsDescendant && disclosureCommentaryCount === 0
  const commentaryBelow = commentaryRectangle.top >= disclosureRectangle.bottom
  const commentaryNonOverlap = !rectanglesOverlap

  if (!commentaryVisible) {
    throw new Error(`Commentary must be visibly rendered: ${commentaryText}`)
  }
  if (!commentaryBelow) {
    throw new Error(
      `Commentary must start at or below the disclosure bottom: ${JSON.stringify({ disclosureRectangle, commentaryRectangle })}`
    )
  }
  if (!commentaryNonOverlap) {
    throw new Error(
      `Commentary must not overlap the Thought disclosure: ${JSON.stringify({ disclosureRectangle, commentaryRectangle })}`
    )
  }

  console.log(
    JSON.stringify(
      {
        count,
        rows,
        nativeSummaryPitches,
        thoughtMetrics,
        ordinaryMetrics: { ...ordinaryMetrics, expectedGap: expectedOrdinaryGap },
        disclosureRectangle,
        commentary: {
          count: exactCommentaryCount,
          text: commentaryText,
          display: commentaryDisplay,
          visible: commentaryVisible,
          rectangle: commentaryRectangle
        },
        commentaryOutside,
        commentaryBelow,
        commentaryNonOverlap
      },
      null,
      2
    )
  )
} finally {
  await browser.close()
}
