import { expect, test } from '@playwright/test'

test('loads the packaged dashboard and preserves browser navigation', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('link', { name: 'RoleSail' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Job inbox' })).toBeVisible()

  await page.getByRole('link', { name: 'Profile' }).click()
  await expect(page).toHaveURL(/\/profile$/)
  await expect(page.getByRole('heading', { name: 'Profile and preferences' })).toBeVisible()

  await page.goBack()
  await expect(page.getByRole('heading', { name: 'Job inbox' })).toBeVisible()
})

test('imports a job and updates the inbox without reloading', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: '+ Add' }).click()
  const url = `https://example.com/jobs/playwright-${Date.now()}`
  await page.getByLabel('Job URL').fill(url)
  await page.getByRole('button', { name: 'Add job' }).click()
  await expect(page.getByRole('status')).toContainText(/Job queued|Fetching job details|Job enriched|Added and scored/)
  await page.getByRole('dialog').getByRole('button', { name: 'Close' }).last().click()
  await expect(page.getByText('Imported job from example.com').first()).toBeVisible()
})
