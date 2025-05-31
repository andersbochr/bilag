import json
import os
import shutil
import argparse

def main():
    # Set up command-line argument parsing
    parser = argparse.ArgumentParser(description="Copy and rename voucher files based on matchinfo.json")
    parser.add_argument("json_file", help="Path to matchinfo.json")
    parser.add_argument("source_dir", help="Directory containing the original files")
    parser.add_argument("dest_dir", help="Directory where the renamed files will be copied")
    args = parser.parse_args()

    # Validate source and destination directories
    if not os.path.exists(args.source_dir) or not os.path.isdir(args.source_dir):
        print(f"Error: Source directory '{args.source_dir}' does not exist or is not a directory.")
        return
    if not os.path.exists(args.dest_dir) or not os.path.isdir(args.dest_dir):
        print(f"Error: Destination directory '{args.dest_dir}' does not exist or is not a directory.")
        return

    # Load and parse the JSON file
    try:
        with open(args.json_file, 'r') as f:
            data = json.load(f)
            matches = data["matches"]
    except FileNotFoundError:
        print(f"Error: JSON file '{args.json_file}' not found.")
        return
    except json.JSONDecodeError:
        print(f"Error: Invalid JSON in '{args.json_file}'.")
        return
    except KeyError:
        print(f"Error: 'matches' key not found in JSON.")
        return

    # Process each voucher ID and its associated file
    for voucher_id, file_list in matches.items():
        if not file_list:
            continue  # Skip if the file list is empty
        original_filename = file_list[0]  # Take the first file in the list
        # Format voucher ID as a 4-digit string with leading zeros
        formatted_id = "{:04d}".format(int(voucher_id))
        # Split the original filename to preserve the extension
        _, extension = os.path.splitext(original_filename)
        # Construct the new filename
        new_filename = f"voucher{formatted_id}{extension}"
        # Build full paths for source and destination
        source_path = os.path.join(args.source_dir, original_filename)
        dest_path = os.path.join(args.dest_dir, new_filename)

        # Check if the source file exists
        if not os.path.exists(source_path):
            print(f"Warning: Source file '{source_path}' not found. Skipping.")
            continue

        # Copy the file to the destination with the new name
        try:
            shutil.copy2(source_path, dest_path)
            print(f"Copied '{source_path}' to '{dest_path}'")
        except Exception as e:
            print(f"Error copying '{source_path}' to '{dest_path}': {e}")

if __name__ == "__main__":
    main()
