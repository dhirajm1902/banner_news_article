import sys
import trafilatura

def fetch(url):
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        print(f"[ERROR] Could not download: {url}")
        return
    text = trafilatura.extract(downloaded)
    if not text:
        print(f"[ERROR] Could not extract text from: {url}")
        return
    print(f"\n{'='*60}")
    print(f"URL: {url}")
    print('='*60)
    print(text)

def main():
    if len(sys.argv) > 1:
        for url in sys.argv[1:]:
            fetch(url)
    else:
        print("Enter URLs one per line. Empty line to process, Ctrl+C to quit.")
        urls = []
        while True:
            try:
                line = input("> ").strip()
                if line:
                    urls.append(line)
                elif urls:
                    for url in urls:
                        fetch(url)
                    urls = []
            except (KeyboardInterrupt, EOFError):
                break

if __name__ == "__main__":
    main()
