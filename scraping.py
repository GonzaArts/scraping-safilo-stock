import os
import time
import csv
import json
import traceback
from dotenv import load_dotenv
from tqdm import tqdm
import concurrent.futures

import undetected_chromedriver as uc
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from bs4 import BeautifulSoup
from woocommerce import API

# Tamaño del bloque de procesamiento
BATCH_SIZE = 100  # Ajusta este valor según tu rendimiento deseado

def login(driver, username, password):
    login_url = 'https://www.youandsafilo.com/es/login?ec=302&startURL=%2F'
    driver.get(login_url)
    handle_cookies_banner(driver)
    try:
        username_field = WebDriverWait(driver, 60).until(
            EC.presence_of_element_located((By.XPATH, '//input[@placeholder="Nombre de usuario"]'))
        )
        password_field = driver.find_element(By.XPATH, '//input[@placeholder="Contraseña"]')
        login_button = driver.find_element(By.XPATH, '//button[@class="login-btn"]')

        username_field.clear()
        username_field.send_keys(username)
        password_field.clear()
        password_field.send_keys(password)
        login_button.click()

        WebDriverWait(driver, 60).until(EC.presence_of_element_located((By.CLASS_NAME, 'swiper-slide')))
        print("Inicio de sesión exitoso")
        return True
    except Exception as e:
        print("Error en el inicio de sesión:", e)
        return False

def handle_cookies_banner(driver):
    try:
        accept_cookies_button = driver.find_element(By.ID, 'onetrust-accept-btn-handler')
        accept_cookies_button.click()
    except NoSuchElementException:
        pass

def read_style_codes(csv_file):
    products = []
    try:
        with open(csv_file, newline='', encoding='utf-8') as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                style_code = row['Style Code'].strip()
                ean_code = row['EAN Code'].strip()
                products.append({'style_code': style_code, 'ean_code': ean_code})
        return products
    except Exception as e:
        print("Error al leer el archivo CSV:", e)
        return []

def extract_ean_and_stock_status(driver, expected_ean):
    try:
        page_source = driver.page_source
        soup = BeautifulSoup(page_source, 'html.parser')
        ean_elements = soup.find_all(string=lambda text: expected_ean in text if text else False)

        if ean_elements:
            for ean_element in ean_elements:
                ean_container = ean_element.find_parent('div')
                if ean_container:
                    availability_container = ean_container.find_next('c-lex-product-availability')
                    stock_status = "Disponible" if availability_container and "Envío rápido" in availability_container.get_text() else "No disponible"
                    return {"ean": expected_ean.lstrip('0'), "stock_status": stock_status, "actualizado": False}
        return {"ean": expected_ean.lstrip('0'), "stock_status": "No disponible", "actualizado": False}
    except Exception as e:
        print(f"Error al buscar EAN y stock: {e}")
        return {"ean": expected_ean.lstrip('0'), "stock_status": "No disponible", "actualizado": False}

def scrape_product_info(driver, style_code, expected_ean):
    product_url = f'https://www.youandsafilo.com/es/product/{style_code}'
    driver.get(product_url)
    handle_cookies_banner(driver)

    current_url = driver.current_url
    if current_url == 'https://www.youandsafilo.com/es/' or "login" in current_url:
        return {"ean": expected_ean.lstrip('0'), "stock_status": "No disponible", "actualizado": False}

    time.sleep(5)
    
    try:
        return extract_ean_and_stock_status(driver, expected_ean)
    except Exception:
        print(f"Error al extraer el producto {style_code}")
        return {"ean": expected_ean.lstrip('0'), "stock_status": "No disponible", "actualizado": False}

def update_stock_in_woocommerce(json_file):
    load_dotenv()
    wc_url = os.getenv('WC_URL')
    wc_consumer_key = os.getenv('WC_CONSUMER_KEY')
    wc_consumer_secret = os.getenv('WC_CONSUMER_SECRET')

    if not wc_url or not wc_consumer_key or not wc_consumer_secret:
        print("Error: Credenciales de WooCommerce no están definidas")
        return

    wcapi = API(
        url=wc_url,
        consumer_key=wc_consumer_key,
        consumer_secret=wc_consumer_secret,
        version="wc/v3",
        timeout=30,
        verify_ssl=True
    )

    with open(json_file, 'r', encoding='utf-8') as f:
        products = json.load(f)

    def update_product(product):
        ean = product['ean']
        stock_status = product['stock_status']

        if product.get('actualizado'):
            return

        try:
            response = wcapi.get("products", params={"sku": ean})

            if response.status_code != 200:
                return

            wc_product = response.json()

            if wc_product:
                wc_product_id = wc_product[0]['parent_id']
                variation_id = wc_product[0]['id']
                stock_quantity = 999 if stock_status == "Disponible" else 0
                data = {"stock_quantity": stock_quantity, "stock_status": "instock" if stock_status == "Disponible" else "outofstock"}

                update_response = wcapi.put(f"products/{wc_product_id}/variations/{variation_id}", data)
                if update_response.status_code in [200, 201]:
                    print(f"Variación {ean} actualizada correctamente.")
                    product['actualizado'] = True
                    with open(json_file, 'w', encoding='utf-8') as jsonfile:
                        json.dump(products, jsonfile, ensure_ascii=False, indent=4)
                else:
                    print(f"Error al actualizar la variación {ean}")
        except Exception as e:
            print(f"Error al actualizar el producto {ean}: {e}")

    products_to_update = [product for product in products if not product.get('actualizado')]
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        executor.map(update_product, products_to_update)

def get_processed_eans(json_file):
    if os.path.exists(json_file):
        with open(json_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return {product['ean'] for product in data}
    return set()

def process_in_batches(driver, products, json_file):
    processed_eans = get_processed_eans(json_file)

    data = []
    if os.path.exists(json_file):
        with open(json_file, 'r', encoding='utf-8') as f:
            data = json.load(f)

    total_products = len(products)
    with tqdm(total=total_products, desc="Procesando productos", unit="producto") as pbar:
        for i in range(0, total_products, BATCH_SIZE):
            batch = products[i:i + BATCH_SIZE]
            for product in batch:
                style_code = product['style_code']
                expected_ean = product['ean_code'].lstrip('0')  # Quitamos el primer '0'
                
                if expected_ean in processed_eans:
                    pbar.update(1)
                    continue  # Saltar productos ya procesados

                # Quitamos el log de "Extrayendo información del producto ..."
                info = scrape_product_info(driver, style_code, expected_ean)
                if info:
                    data.append(info)
                    processed_eans.add(expected_ean)
                    with open(json_file, 'w', encoding='utf-8') as jsonfile:
                        json.dump(data, jsonfile, ensure_ascii=False, indent=4)
                pbar.update(1)
                time.sleep(2)

def main():
    load_dotenv()
    username = os.getenv('LOGIN_USERNAME')
    password = os.getenv('LOGIN_PASSWORD')

    if not username or not password:
        print("Error: Las credenciales no están definidas")
        return

    csv_file = 'style_codes.csv'
    json_file = 'product_data.json'

    products = read_style_codes(csv_file)
    if not products:
        print("No se encontraron productos")
        return

    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("--start-maximized")

    driver = uc.Chrome(options=chrome_options)

    if not login(driver, username, password):
        driver.quit()
        return

    process_in_batches(driver, products, json_file)

    driver.quit()
    update_stock_in_woocommerce(json_file)
    print("Actualización de stock en WooCommerce completada.")

if __name__ == "__main__":
    main()
