import os
from ruamel.yaml import YAML
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def get_chart_image_values(folder):
    '''
    Get paths of values.yaml or values.yml files in the specified folder.

    :param folder: The root folder to search for values files
    :return: List of paths to values files
    '''
    chart_image_yaml_files = []
    for root, dirs, files in os.walk(folder):
        for file in files:
            if file == 'values.yaml' or file == 'values.yml':
                chart_image_yaml_files.append(os.path.join(root, file))
    logger.info(f"Found {len(chart_image_yaml_files)} values files in {folder}")
    return chart_image_yaml_files

def find_images(content, public_image, private_image, chart_values, parent_keys=[]):
    '''
    Recursively find images in the given content dictionary and create a nested dictionary of key-value pairs.

    :param content: Dictionary representing the YAML content
    :param image: Image name to search for in the content
    :param private_image: Private image URL to replace the public image
    :param chart_values: Dictionary to store key-value pairs
    :param parent_keys: List to track the nested keys
    '''

    public_image_repo = public_image.split(':')[0]
    private_image_repo, private_image_tag = private_image.split(':')

    # Split private repo into registry host and repo path (host/path:tag)
    private_registry = ""
    private_repo_path = private_image_repo
    if '/' in private_image_repo:
        parts = private_image_repo.split('/', 1)
        private_registry = parts[0]
        private_repo_path = parts[1]

    def _ensure_path(target: dict, keys: list[str]) -> dict:
        d = target
        for k in keys:
            d = d.setdefault(k, {})
        return d

    if isinstance(content, dict):
        for key, value in content.items():
            current_keys = parent_keys + [key]
            if isinstance(value, str) and public_image_repo in value:
                # Decide how to write overlay based on sibling keys (schema detection)
                parent_obj = content  # dict that holds the key
                # Case A: parent object contains registry split fields under an 'image' object
                # e.g., image: { registry: ..., repository|image|name: ..., tag: ... }
                # When key is "repository" or "image" or "name", the parent of this dict is the 'image' key one level up.
                if any(k in parent_obj for k in ("registry", "repository", "image", "name")):
                    # Navigate overlay to the parent of this object (drop the last key and set under that object)
                    # e.g., ... -> image
                    d = _ensure_path(chart_values, current_keys[:-2])
                    image_obj_key = current_keys[-2]
                    # Build overlay payload honoring schema
                    payload = {}
                    if "registry" in parent_obj:
                        payload["registry"] = private_registry or private_image_repo.split('/')[0]
                        # choose repo field name
                        repo_field = "repository" if "repository" in parent_obj else ("image" if "image" in parent_obj else ("name" if "name" in parent_obj else "repository"))
                        payload[repo_field] = private_repo_path if private_registry else private_image_repo
                    else:
                        # No explicit registry in schema; put full repo in repository/name
                        repo_field = "repository" if "repository" in parent_obj else ("image" if "image" in parent_obj else ("name" if "name" in parent_obj else "repository"))
                        payload[repo_field] = private_image_repo
                    # tag if present in chart
                    if "tag" in parent_obj:
                        payload["tag"] = private_image_tag
                    else:
                        # Still set tag to be explicit
                        payload["tag"] = private_image_tag
                    d[image_obj_key] = payload
                    logger.info(f"Found {'.'.join(current_keys)}: {value}")
                # Case B: flattened keys on same level (imageRegistry/imageRepository/imageTag)
                elif any(k in parent_obj for k in ("imageRegistry", "imageRepository", "imageTag")) or key in ("imageRepository", "imageRegistry", "imageTag"):
                    # Write keys at current parent path (drop just the last key)
                    d = _ensure_path(chart_values, current_keys[:-1])
                    d["imageRegistry"] = private_registry or private_image_repo.split('/')[0]
                    d["imageRepository"] = private_repo_path if private_registry else private_image_repo
                    d["imageTag"] = private_image_tag
                    logger.info(f"Found {'.'.join(current_keys)}: {value}")
                # Case C: generic repository/tag pair on same parent
                elif "repository" in parent_obj or key == "repository":
                    d = _ensure_path(chart_values, current_keys[:-1])
                    d["repository"] = private_image_repo
                    d["tag"] = private_image_tag
                    logger.info(f"Found {'.'.join(current_keys)}: {value}")
                else:
                    # Fallback: set under parent object if present, else set directly
                    d = _ensure_path(chart_values, current_keys[:-2])
                    parent_key = current_keys[-2] if len(current_keys) >= 2 else current_keys[-1]
                    d[parent_key] = {
                        'repository': private_image_repo,
                        'tag': private_image_tag
                    }
                    logger.info(f"Found {'.'.join(current_keys)}: {value}")
            elif isinstance(value, dict):
                # Detect split registry/repo schemas at this dict level and write overlay if matching
                try:
                    # Case A: value has registry + (repository|image|name)
                    repo_key = "repository" if "repository" in value else ("image" if "image" in value else ("name" if "name" in value else None))
                    if "registry" in value and repo_key and isinstance(value.get("registry"), str) and isinstance(value.get(repo_key), str):
                        composed_src = f"{value.get('registry')}/{value.get(repo_key)}"
                        if composed_src == public_image_repo:
                            d = _ensure_path(chart_values, current_keys[:-1])
                            payload = {}
                            payload["registry"] = private_registry or private_image_repo.split('/')[0]
                            payload[repo_key] = private_repo_path if private_registry else private_image_repo
                            # Set tag explicitly
                            payload["tag"] = private_image_tag
                            d[current_keys[-1]] = payload
                            logger.info(f"Found {'.'.join(current_keys)}: registry+{repo_key} matched {public_image_repo}")
                    # Case B: flattened imageRegistry/imageRepository/imageTag
                    if isinstance(value.get("imageRegistry"), str) and isinstance(value.get("imageRepository"), str):
                        composed_src = f"{value.get('imageRegistry')}/{value.get('imageRepository')}"
                        if composed_src == public_image_repo:
                            d = _ensure_path(chart_values, current_keys[:-1])
                            d["imageRegistry"] = private_registry or private_image_repo.split('/')[0]
                            d["imageRepository"] = private_repo_path if private_registry else private_image_repo
                            d["imageTag"] = private_image_tag
                            logger.info(f"Found {'.'.join(current_keys)}: imageRegistry/imageRepository matched {public_image_repo}")
                except Exception:
                    # Non-fatal; continue recursion
                    pass
                find_images(value, public_image, private_image, chart_values, current_keys)
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    find_images(item, public_image, private_image, chart_values, current_keys + [str(index)])
    elif isinstance(content, list):
        for index, item in enumerate(content):
            find_images(item, public_image, private_image, chart_values, parent_keys + [str(index)])
    return chart_values

def extract_chart_values_image(chart_image_yaml_file, public_images, private_images):
    '''
    Extract Docker image information from the values file of a Helm chart.

    :param chart_image_yaml_file: Path to the values.yaml file
    :param public_images: List of public Docker images
    :param private_images: List of private Docker images
    :return: Dictionary containing key-value pairs of found images
    '''
    yaml = YAML()
    with open(chart_image_yaml_file, 'r', encoding='utf-8', errors='replace') as file:
        logger.info(f"Extracting Images from {chart_image_yaml_file}")
        content = yaml.load(file)
        
        # Create a dictionary to store key-value pairs of found images
        chart_values = {}

        print(f"Searching for images: {public_images}")

        try:
            # Build explicit mapping of public -> private by index and process all entries
            mapping = {}
            limit = min(len(public_images), len(private_images))
            for i in range(limit):
                mapping[public_images[i]] = private_images[i]
            for pub, priv in mapping.items():
                chart_values = find_images(content, pub, priv, chart_values)
        except Exception as e:
            logger.error(f"Error processing images: {e}")

        print(chart_values)
        return chart_values

def convert_dict_to_yaml(chart_values, output_file):
    '''
    Convert the dictionary of chart values to a YAML file.

    :param chart_values: Dictionary containing key-value pairs of found images
    :param output_file: Path to the output YAML file
    '''
    yaml = YAML()
    
    with open(output_file, 'w', encoding='utf-8') as file:
        yaml.dump(chart_values, file)

    logger.info(f"Updated values saved to {output_file}")
