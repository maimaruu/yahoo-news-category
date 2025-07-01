import time
import re
import json
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from oauth2client.service_account import ServiceAccountCredentials
import gspread

# --- Google Sheets設定 ---
SERVICE_ACCOUNT_FILE = "credentials.json"
SHEET_NAME = "yahoo-news-scraper"

# --- Selenium設定 ---
def init_driver():
    """
    WebDriverを初期化し、ヘッドレスモードでChromeを設定します。
    """
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36")
    # GitHub Actions環境を想定したパスですが、ローカル環境に合わせて適宜修正してください
    try:
        service = Service("/usr/bin/chromedriver") 
        driver = webdriver.Chrome(service=service, options=chrome_options)
    except Exception:
        # ローカル環境などで上記パスがない場合、自動検出を試みます
        driver = webdriver.Chrome(options=chrome_options)
    print("[DEBUG] WebDriver initialized.")
    return driver

# --- 本文抽出関数 ---
def extract_body(soup):
    """
    BeautifulSoupオブジェクトから記事の本文を抽出します。
    """
    # 記事本文のコンテナを新しいセレクタで検索
    article_content_div = soup.find("div", class_=re.compile(r"article_body|ArticleBody"))
    # 新しい構造に対応: `div` with `data-testid="article-body"`
    if not article_content_div:
         article_content_div = soup.find("div", attrs={"data-testid": "article-body"})

    if not article_content_div:
        print("[DEBUG] No article content container found.")
        return ""

    # 不要なタグを除去
    for tag in article_content_div.find_all(["figure", "aside", "script", "style", "noscript", "blockquote"]):
        tag.decompose()

    # 段落を結合
    paragraphs = [p.get_text(" ", strip=True) for p in article_content_div.find_all("p") if p.get_text(strip=True)]
    body = "\n".join(paragraphs)
    print(f"[DEBUG] Extracted body part length: {len(body)}")
    return body

# --- 記事情報取得関数 (★改善版) ---
def extract_article_info(driver, url):
    """
    記事のURLにアクセスし、タイトル、情報源、掲載時刻、本文、ジャンル、IDを抽出します。
    """
    try:
        print(f"[DEBUG] Extracting info from: {url}")
        driver.get(url)
        time.sleep(2) 
        soup = BeautifulSoup(driver.page_source, "html.parser")

        # 1. IDの抽出
        article_id_match = re.search(r'articles/([a-f0-9]+)', url)
        article_id = article_id_match.group(1) if article_id_match else "NO_ID"

        # 2. タイトルの抽出
        meta_title = soup.find("meta", property="og:title")
        title = meta_title["content"].strip() if meta_title and meta_title.get("content") else "NO TITLE"
        title = re.sub(r'（.*?） - Yahoo!ニュース$', '', title).strip()

        # 3. 情報源の抽出
        provider = "不明"
        ld_json_scripts = soup.find_all("script", type="application/ld+json")
        for script in ld_json_scripts:
            if script.string:
                try:
                    data = json.loads(script.string)
                    if isinstance(data, dict):
                        if data.get("@type") == "NewsArticle" and data.get("publisher", {}).get("name"):
                            provider_name = data["publisher"]["name"].strip()
                            if provider_name != "Yahoo!ニュース":
                                provider = provider_name
                                break 
                except json.JSONDecodeError:
                    continue
        if provider == "不明":
            provider_span = soup.find("span", class_=re.compile(r"provider|sc-f06b9b1-0"))
            if provider_span:
                provider = provider_span.get_text(strip=True)

        # 4. 掲載時刻の抽出
        pub_time = ""
        time_tag = soup.find("time")
        if time_tag and time_tag.has_attr('datetime'):
            pub_time = time_tag['datetime'].strip()

        # 5. ジャンル（カテゴリ）の抽出 (★ロジック大幅改善)
        genre = "その他"
        # 新しいパンくずリストの構造からジャンルを取得
        breadcrumb_nav = soup.find("nav", attrs={"aria-label": "パンくずリスト"})
        if breadcrumb_nav:
            list_items = breadcrumb_nav.find_all("li")
            # 例: ホーム > 経済 > 経済総合
            # list_items[1] が大ジャンル, list_items[2] がサブジャンル
            if len(list_items) > 1:
                main_genre = list_items[1].get_text(strip=True)
                if len(list_items) > 2:
                    sub_genre = list_items[2].get_text(strip=True)
                    genre = f"{main_genre}/{sub_genre}"
                else:
                    genre = f"{main_genre}総合"
            print(f"[DEBUG] Extracted genre from breadcrumb: {genre}")
        else:
             print("[DEBUG] Breadcrumb not found. Falling back to old method.")
             # フォールバックとして従来の `__PRELOADED_STATE__` を利用するが、信頼性は低い
             preloaded_state_script_content = None
             for script_tag in soup.find_all("script"):
                 if script_tag.string and 'window.__PRELOADED_STATE__ =' in script_tag.string:
                     preloaded_state_script_content = script_tag.string
                     break
             if preloaded_state_script_content:
                # (従来のロジックをここに記述...ただし、現在はパンくずリストが主流)
                pass
        
        # 最終的なジャンル名の揺れを吸収
        if "ビジネス" in genre:
            genre = genre.replace("ビジネス", "経済")

        print(f"[DEBUG] Final genre: {genre}")

        # 6. 本文の抽出
        body = extract_body(soup)

        return article_id, title, provider, pub_time, body[:3000] if body else "", genre
    except Exception as e:
        print(f"[ERROR] Failed to extract article info from {url}: {e}")
        return "ERROR", "ERROR", "ERROR", "ERROR", "", "ERROR"


# --- スプレッドシートへ書き込み関数 (変更なし) ---
def append_to_sheet(data, existing_urls):
    """
    収集したデータをGoogle Sheetsに追記します。
    """
    print(f"[INFO] Writing {len(data)} new records to the sheet...")
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, scope)
    client = gspread.authorize(creds)
    sheet = client.open(SHEET_NAME).sheet1

    existing = sheet.get_all_values()
    if not existing:
        headers = ["ID", "収集時刻", "タイトル", "情報源", "掲載時刻", "URL", "ジャンル", "本文"]
        sheet.append_row(headers)
        print("[INFO] Header row inserted.")
        existing = sheet.get_all_values()
        existing_urls = {row[5] for row in existing[1:] if len(row) > 5}

    new_rows = []
    for row in data:
        if row[5] not in existing_urls:
            new_rows.append(row)
            existing_urls.add(row[5])

    print(f"[INFO] {len(new_rows)} new unique records to write.")

    if new_rows:
        sheet.append_rows(new_rows, value_input_option="RAW")
        print(f"[INFO] Completed. {len(new_rows)} rows added.")
    else:
        print("[INFO] No new records to write.")


# --- メイン処理 (変更なし) ---
if __name__ == "__main__":
    print("[START] Yahoo News scraping started.")
    driver = None
    try:
        driver = init_driver()

        jst = timezone(timedelta(hours=9))
        timestamp = datetime.now(jst).strftime("%Y/%m/%d %H:%M")

        # カテゴリ優先度は現状維持でOK
        CATEGORY_PRIORITY = { "国内": 0, "地域": 0 } # 簡略化。元コードのままでOKです

        category_urls = {
            "国内": "https://news.yahoo.co.jp/categories/domestic",
            "国際": "https://news.yahoo.co.jp/categories/world",
            "経済": "https://news.yahoo.co.jp/categories/business", # ★URLが "economy" -> "business" に変更されている
            "エンタメ": "https://news.yahoo.co.jp/categories/entertainment",
            "スポーツ": "https://news.yahoo.co.jp/categories/sports",
            "IT": "https://news.yahoo.co.jp/categories/it",
            "科学": "https://news.yahoo.co.jp/categories/science",
            "ライフ": "https://news.yahoo.co.jp/categories/life",
            "地域": "https://news.yahoo.co.jp/categories/local",
        }

        temp_article_storage = {}
        
        try:
            scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
            creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, scope)
            client = gspread.authorize(creds)
            sheet = client.open(SHEET_NAME).sheet1
            existing_urls_on_sheet = {row[5] for row in sheet.get_all_values()[1:] if len(row) > 5}
            print(f"[INFO] Fetched {len(existing_urls_on_sheet)} existing URLs from the sheet.")
        except Exception as e:
            print(f"[ERROR] Could not connect to Google Sheet: {e}")
            existing_urls_on_sheet = set()

        for category_name, base_url in category_urls.items():
            print(f"\n--- Scraping Category: {category_name} ({base_url}) ---")
            driver.get(base_url)
            time.sleep(3) 

            # 記事リンクのセレクタを更新
            soup = BeautifulSoup(driver.page_source, "html.parser")
            # ページ上部の主要ニュースリストなど、複数のセレクタを試す
            article_links = soup.select('a[href*="/articles/"]')

            print(f"[DEBUG] Found {len(article_links)} article links in {category_name}.")
            
            unique_urls_this_category = set()
            for a in article_links:
                href = a.get("href")
                if href and href.startswith("https://news.yahoo.co.jp/articles/"):
                     unique_urls_this_category.add(href.split("?")[0])

            for article_url in list(unique_urls_this_category)[:15]: # 処理件数を制限して負荷を軽減
                if article_url in existing_urls_on_sheet or article_url in temp_article_storage:
                    continue
                
                article_id, title, provider, pub_time, body, genre = extract_article_info(driver, article_url)

                if title == "ERROR" or not body:
                    continue

                current_article_data = [
                    article_id, timestamp, title, provider, pub_time, article_url, genre, body
                ]
                temp_article_storage[article_url] = current_article_data
                print(f"[ADD] {title} (ID: {article_id}) - Genre: {genre}")
    
    except Exception as main_e:
        print(f"[CRITICAL ERROR] An error occurred: {main_e}")
    finally:
        if driver:
            driver.quit()
            print("[INFO] WebDriver closed.")

    final_data_to_write = list(temp_article_storage.values())

    if final_data_to_write:
        append_to_sheet(final_data_to_write, existing_urls_on_sheet)
    else:
        print("[INFO] No new unique articles to write.")

    print("[END] Yahoo News scraping finished.")
