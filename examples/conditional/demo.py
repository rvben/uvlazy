import argparse

parser = argparse.ArgumentParser(
    description="Only the branch you choose installs its dependencies."
)
parser.add_argument("mode", choices=["plain", "pretty", "image"], default="plain", nargs="?")
args = parser.parse_args()

if args.mode == "pretty":
    from rich import print

    print("[bold green]rich was installed at this import.[/bold green]")
elif args.mode == "image":
    from PIL import Image

    picture = Image.new("RGB", (32, 32), "coral")
    print(f"Pillow was installed at this import. Image size: {picture.size}")
else:
    print("Hello! No third-party dependencies needed or installed.")
