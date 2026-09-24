"""
Playwright Integration Tests for Radio TEDU Broadcast Tool
Tests the AI Host settings and functionality through the browser.
"""

import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright, Page

BASE_URL = "http://127.0.0.1:8100"
ADMIN_USER = "admin"
ADMIN_PASSWORD = "changeme"


async def login(page: Page):
    """Login to the application."""
    await page.goto(f"{BASE_URL}/login.html")
    await page.wait_for_load_state("networkidle")
    
    # Fill login form
    await page.fill('input[name="username"]', ADMIN_USER)
    await page.fill('input[name="password"]', ADMIN_PASSWORD)
    await page.click('button[type="submit"]')
    
    # Wait for redirect
    await page.wait_for_timeout(1000)
    
    # Verify we're logged in
    current_url = page.url
    assert "login" not in current_url or "app" in current_url, f"Login failed, stayed at {current_url}"


async def test_ai_settings_page(page: Page):
    """Test the AI settings menu."""
    print("\n=== Testing AI Settings Page ===")
    
    # Navigate to app
    await page.goto(f"{BASE_URL}/app?station_id=1")
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(2000)
    
    # Check if AI menu/settings exist
    ai_settings_exists = await page.locator("#aiSettings, [class*='ai-settings'], [data-testid='ai-settings']").count() > 0
    
    if ai_settings_exists:
        print("✅ AI Settings page found")
        
        # Test AI toggle
        ai_toggle = page.locator("#aiHostToggle, [data-testid='ai-toggle']")
        if await ai_toggle.count() > 0:
            print("✅ AI Host toggle found")
            
            # Toggle AI on
            is_checked = await ai_toggle.is_checked()
            if not is_checked:
                await ai_toggle.click()
                await page.wait_for_timeout(500)
                print("✅ AI Host enabled via toggle")
            
            # Test settings fields
            fields = [
                "#llmModel",
                "#ttsModelPath",
                "#voicePersona",
                "#announcementLength"
            ]
            
            for field_selector in fields:
                exists = await page.locator(field_selector).count() > 0
                status = "✅" if exists else "⚠️"
                print(f"{status} Field {field_selector}: {'found' if exists else 'not found'}")
        else:
            print("⚠️ AI Host toggle not found")
    else:
        print("⚠️ AI Settings page not found - may need to be created")


async def test_ai_announcement_generation(page: Page):
    """Test that AI generates announcements for tracks."""
    print("\n=== Testing AI Announcement Generation ===")
    
    console_messages = []
    page.on("console", lambda msg: console_messages.append({
        "type": msg.type,
        "text": msg.text
    }))
    
    # Navigate to app and wait for broadcast
    await page.goto(f"{BASE_URL}/app?station_id=1")
    await page.wait_for_load_state("networkidle")
    
    # Wait for 20 seconds of broadcast
    print("Monitoring broadcast for 20 seconds...")
    for i in range(20):
        await page.wait_for_timeout(1000)
        if i % 5 == 0:
            print(f"  ... {i+1}s elapsed")
    
    # Check console for AI-related messages
    ai_messages = [m for m in console_messages if "ai" in m["text"].lower() or "announcement" in m["text"].lower()]
    
    if ai_messages:
        print(f"✅ Found {len(ai_messages)} AI-related console messages")
        for msg in ai_messages[:3]:
            print(f"  - [{msg['type']}] {msg['text'][:100]}")
    else:
        print("⚠️ No AI messages detected (AI may not be enabled)")


async def test_ai_status_api(page: Page):
    """Test the AI status API endpoint."""
    print("\n=== Testing AI Status API ===")
    
    await page.goto(f"{BASE_URL}/app?station_id=1")
    await page.wait_for_load_state("networkidle")
    
    # Check AI status via API
    response = await page.evaluate("""
        async () => {
            const token = localStorage.getItem('access_token');
            const res = await fetch('/api/ai/status', {
                headers: { 'Authorization': `Bearer ${token}` }
            });
            return res.json();
        }
    """)
    
    print(f"AI Status Response: {json.dumps(response, indent=2)}")
    
    if response.get("detail") and "not found" in str(response["detail"]).lower():
        print("⚠️ AI Status API endpoint not implemented yet")
    else:
        print("✅ AI Status API endpoint exists")


async def main():
    """Run all Playwright tests."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        
        # Collect all console messages
        page.on("console", lambda msg: print(f"[BROWSER] {msg.type}: {msg.text}"))
        page.on("pageerror", lambda err: print(f"[ERROR] {err}"))
        
        try:
            # Login
            print("Logging in...")
            await login(page)
            print("✅ Logged in successfully")
            
            # Run tests
            await test_ai_settings_page(page)
            await test_ai_announcement_generation(page)
            await test_ai_status_api(page)
            
            print("\n" + "="*60)
            print("  Playwright Tests Complete")
            print("="*60)
            
        except Exception as e:
            print(f"\n❌ Test failed: {e}")
            import traceback
            traceback.print_exc()
            
            # Save screenshot on error
            await page.screenshot(path="playwright_error.png")
            print("Screenshot saved to playwright_error.png")
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
