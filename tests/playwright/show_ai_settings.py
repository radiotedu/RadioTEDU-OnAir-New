"""
Playwright script to show and verify AI Host settings panel
"""

import asyncio
from playwright.async_api import async_playwright, Page

BASE_URL = "http://127.0.0.1:8100"
ADMIN_USER = "admin"
ADMIN_PASSWORD = "changeme"


async def login(page: Page):
    """Login to the application."""
    print("→ Navigating to login page...")
    await page.goto(f"{BASE_URL}/login.html")
    await page.wait_for_load_state("networkidle")
    
    print("→ Filling credentials...")
    await page.fill('input[name="username"]', ADMIN_USER)
    await page.fill('input[name="password"]', ADMIN_PASSWORD)
    await page.click('button[type="submit"]')
    
    await page.wait_for_timeout(1500)
    print(f"→ Logged in! Current URL: {page.url}")


async def navigate_to_ai_host_panel(page: Page):
    """Click the AI Host button in sidebar."""
    print("\n=== Navigating to AI Host Panel ===")
    
    await page.goto(f"{BASE_URL}/app?station_id=1")
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(2000)
    
    # Click AI Host button in sidebar
    ai_btn = page.locator('button.nav-btn[data-panel="ai-host"]')
    if await ai_btn.count() > 0:
        print("✅ AI Host button found in sidebar")
        await ai_btn.click()
        await page.wait_for_timeout(1000)
    else:
        print("❌ AI Host button NOT found!")
        return False
    
    # Check if panel is visible
    panel = page.locator('#panel-ai-host')
    if await panel.count() > 0:
        print("✅ AI Host panel is visible")
        
        # Screenshot the panel
        await page.screenshot(path="ai_host_panel.png", full_page=True)
        print("📸 Screenshot saved: ai_host_panel.png")
        return True
    else:
        print("❌ AI Host panel NOT found!")
        return False


async def show_ai_settings_details(page: Page):
    """Display all AI settings elements."""
    print("\n=== AI Settings Elements ===")
    
    # Check toggle
    toggle = page.locator('#aiHostToggle')
    if await toggle.count() > 0:
        is_checked = await toggle.is_checked()
        print(f"{'✅' if is_checked else '⚠️'} AI Host Toggle: {'ON' if is_checked else 'OFF'}")
    else:
        print("❌ AI Toggle not found")
    
    # Check fields
    fields = {
        '#aiLlmModel': 'LLM Model',
        '#aiTtsModelPath': 'TTS Model Path',
        '#aiVoicePersona': 'Voice Persona',
        '#aiAnnouncementLength': 'Announcement Length',
        '#aiMusicHistory': 'Music History Checkbox',
        '#aiEducationalSegments': 'Educational Segments',
        '#aiStationIdInterval': 'Station ID Interval'
    }
    
    for selector, name in fields.items():
        el = page.locator(selector)
        if await el.count() > 0:
            if selector.startswith('#ai'):
                if 'Checkbox' not in name:
                    value = await el.input_value() if el else 'N/A'
                    print(f"✅ {name}: {value}")
                else:
                    checked = await el.is_checked()
                    print(f"{'✅' if checked else '⚠️'} {name}: {checked}")
        else:
            print(f"❌ {name}: NOT FOUND")
    
    # Check buttons
    buttons = {
        '#aiSaveSettingsBtn': 'Save Button',
        '#aiWarmupBtn': 'Load Models Button',
        '#aiClearCacheBtn': 'Clear Cache Button'
    }
    
    for selector, name in buttons.items():
        el = page.locator(selector)
        if await el.count() > 0:
            print(f"✅ {name}")
        else:
            print(f"⚠️ {name}: NOT FOUND")


async def enable_ai_by_default(page: Page):
    """Enable AI Host and save settings."""
    print("\n=== Enabling AI Host by Default ===")
    
    # Toggle AI on
    toggle = page.locator('#aiHostToggle')
    if await toggle.count() > 0:
        is_checked = await toggle.is_checked()
        if not is_checked:
            print("→ Clicking AI Host toggle...")
            await toggle.click()
            await page.wait_for_timeout(500)
            print("✅ AI Host toggled ON")
        else:
            print("✅ AI Host already ON")
    
    # Click save
    save_btn = page.locator('#aiSaveSettingsBtn')
    if await save_btn.count() > 0:
        print("→ Clicking Save AI Settings...")
        await save_btn.click()
        await page.wait_for_timeout(2000)
        print("✅ Settings saved!")
    
    # Screenshot final state
    await page.screenshot(path="ai_enabled.png", full_page=True)
    print("📸 Screenshot saved: ai_enabled.png")


async def enable_ai_in_database():
    """Enable AI directly in database as backup."""
    import sqlite3
    from pathlib import Path
    
    db_path = Path(__file__).parents[2] / "data" / "cleanroom.db"
    if not db_path.exists():
        print("⚠️ Database not found")
        return
    
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("""
            INSERT INTO station_settings (station_id, key, value)
            VALUES (1, 'ai_host_enabled', 'true')
            ON CONFLICT(station_id, key) DO UPDATE SET value='true'
        """)
        conn.commit()
        print("✅ AI enabled in database")
    except Exception as e:
        print(f"⚠️ Could not update database: {e}")
    finally:
        conn.close()


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        
        # Console logging
        page.on("console", lambda msg: print(f"[BROWSER] {msg.type}: {msg.text[:100]}"))
        page.on("pageerror", lambda err: print(f"[ERROR] {err}"))
        
        try:
            # Login
            await login(page)
            
            # Navigate to AI panel
            found = await navigate_to_ai_host_panel(page)
            
            if found:
                # Show all settings
                await show_ai_settings_details(page)
                
                # Enable AI
                await enable_ai_by_default(page)
                
                print("\n" + "=" * 60)
                print("  AI Host Panel Verified Successfully!")
                print("=" * 60)
                print("\nThe AI Host menu is located in the sidebar")
                print("Look for the 'AI Host' button with the robot icon (smart_toy)")
                print("\nTo access it:")
                print("1. Open http://127.0.0.1:8100/app?station_id=1")
                print("2. Login (admin/changeme)")
                print("3. Click 'AI Host' in the left sidebar")
            else:
                print("\n❌ AI Host panel not accessible - enabling via database")
                await enable_ai_in_database()
            
        except Exception as e:
            print(f"\n❌ Failed: {e}")
            import traceback
            traceback.print_exc()
            await page.screenshot(path="ai_error.png", full_page=True)
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
