import os
import json
import whisper


def transcribe_audio(file_path, model_name="base"):
    """
    Transcribes an audio file using Whisper.

    Parameters:
      file_path (str): Path to the MP3 file.
      model_name (str): Which Whisper model to use ("tiny", "base", "small", "medium", "large").
                        "base" is a good starting point.

    Returns:
      str: The transcribed text.
    """
    model = whisper.load_model(model_name)
    result = model.transcribe(file_path)
    return result["text"]


def main():
    # Directory containing your MP3 files
    records_dir = "records"
    # Output JSON file to store transcripts
    output_json = "transcripts.json"

    transcripts = {}

    # Loop over all MP3 files in the records folder
    for filename in os.listdir(records_dir):
        if filename.lower().endswith(".mp3"):
            file_path = os.path.join(records_dir, filename)
            print(f"Transcribing {filename}...")
            try:
                text = transcribe_audio(file_path)
                transcripts[filename] = text
                print(f"Transcription for {filename} complete.")
            except Exception as e:
                print(f"Error transcribing {filename}: {e}")

    # Save the transcripts to a JSON file
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(transcripts, f, ensure_ascii=False, indent=4)

    print(f"All transcripts saved to {output_json}")


if __name__ == "__main__":
    main()
