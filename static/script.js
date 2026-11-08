
// Destination photo gallery

let currentGalleryImages = [];
let currentGalleryIndex = 0;

function openGallery(image) {
    const card = image.closest('.recommendation-card');

    currentGalleryImages = Array.from(
        card.querySelectorAll('.recommendation-image img, .destination-gallery img')
    );

    currentGalleryIndex = currentGalleryImages.indexOf(image);

    if (currentGalleryIndex === -1) return;

    showGalleryImage();
    document.getElementById('galleryModal').classList.add('active');
}

function showGalleryImage() {
    document.getElementById('galleryLargeImage').src =
        currentGalleryImages[currentGalleryIndex].src;

    document.getElementById('galleryCounter').textContent =
        `${currentGalleryIndex + 1} / ${currentGalleryImages.length}`;
}

function changeGallery(direction, event) {
    if (event) event.stopPropagation();
    if (!currentGalleryImages.length) return;

    currentGalleryIndex =
        (currentGalleryIndex + direction + currentGalleryImages.length)
        % currentGalleryImages.length;

    showGalleryImage();
}

function closeGallery(event) {
    if (event) {
        if (
            event.target.id !== 'galleryModal' &&
            !event.target.classList.contains('gallery-close')
        ) return;
        event.stopPropagation();
    }

    document.getElementById('galleryModal').classList.remove('active');
}
