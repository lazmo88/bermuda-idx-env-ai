"""Trilateration calculations for Bermuda."""

from typing import List, Tuple
import numpy as np

def trilaterate(positions: List[Tuple[float, float, float]], distances: List[float]) -> Tuple[float, float, float]:
    """
    Perform trilateration to estimate the position of a device.

    Args:
    positions (List[Tuple[float, float, float]]): List of (x, y, z) coordinates of the scanners.
    distances (List[float]): List of distances from each scanner to the device.

    Returns:
    Tuple[float, float, float]: Estimated (x, y, z) position of the device.
    """
    A = 2 * np.array([positions[i] for i in range(1, len(positions))]) - 2 * np.array(positions[0])
    b = np.array([distances[0]**2 - distances[i]**2 - np.sum(positions[0]**2) + np.sum(positions[i]**2) for i in range(1, len(positions))])
    
    try:
        estimated_position = np.linalg.lstsq(A, b, rcond=None)[0]
        return tuple(estimated_position)
    except np.linalg.LinAlgError:
        return None

def calculate_accuracy(estimated_position: Tuple[float, float, float], actual_position: Tuple[float, float, float]) -> float:
    """
    Calculate the accuracy of the estimated position.

    Args:
    estimated_position (Tuple[float, float, float]): Estimated position of the device.
    actual_position (Tuple[float, float, float]): Actual position of the device (if known).

    Returns:
    float: The Euclidean distance between the estimated and actual positions.
    """
    return np.linalg.norm(np.array(estimated_position) - np.array(actual_position))
